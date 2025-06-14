import os, json, re, math, argparse, random, numpy as np
from pathlib import Path

import torch
from datasets import Dataset
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    TrainerCallback,
)
from trl.trainer import GRPOConfig, GRPOTrainer

def parse_args():
    p = argparse.ArgumentParser(description="GAINRL (GRPO) fine-tuning script")
    p.add_argument("--dataset",       required=True, help="Name of dataset")
    p.add_argument("--dataset_path",  required=True, help="Path to dataset")
    p.add_argument("--indices_path",  required=True, help="Path to sorted data indices")
    
    p.add_argument("--model_name", default="Qwen/Qwen2.5-0.5B-Instruct", help="HF Hub model name or local directory")
    p.add_argument("--gpu_id", default="0",  help="CUDA device ID to use")
    p.add_argument("--output_dir", default=None, help="Output directory (auto-generated by default)")
    # ★ Training hyperparameters
    p.add_argument("--learning_rate",  type=float, default=1e-6,  help="Learning rate")
    p.add_argument("--batch_size",     type=int,   default=16,    help="Batch size per update")
    p.add_argument("--num_epochs",     type=int,   default=200,   help="Number of training epochs")
    p.add_argument("--total_loops",    type=int,   default=200,   help="Total number of sampling loops for DatasetUpdateCallback")
    p.add_argument("--subset_size",    type=int,   default=256,  help="Number of samples to draw each loop")
    p.add_argument("--max_prompt_len", type=int,   default=512,   help="Maximum prompt length")
    p.add_argument("--max_completion_len",type=int, default=512,  help="Maximum generation (completion) length")
    p.add_argument("--num_generations",type=int,    default=8,    help="The number of times grpo answered a single question")
    p.add_argument("--gradient_accumulation_steps", type=int, default=16, help="The number of gradient_accumulation_steps")
    p.add_argument("--save_steps",     type=int,   default=20,    help="The number of save_steps")
    p.add_argument("--logging_steps",  type=int,   default=5,     help="The number of save_steps")
    return p.parse_args()



R1_STYLE_SYSTEM_PROMPT = """A conversation between User and Assistant. The user asks a question, and the Assistant solves it.
The assistant first thinks about the reasoning process in the mind and then provides the user
with the answer. Let's think step by step and output the final answer within \\boxed{}."""

def preprocess_dataset(dataset: Dataset, chunk_size: int = 1000) -> Dataset:
    def process_batch(batch):
        prompts = [[
            {'role': 'system', 'content': R1_STYLE_SYSTEM_PROMPT},
            {'role': 'user', 'content': "What is 2+2?"},
            {'role': 'assistant', 'content': "To calculate 2+2, we simply add the numbers together: 2 + 2 = 4. The answer is \\boxed{4}"},
            {'role': 'user', 'content': q.strip()}
            
        ] for q in batch['problem']]
        return {"prompt": prompts, "answer": batch['answer']}
    return dataset.map(process_batch, batched=True, batch_size=chunk_size)



from transformers import TrainerCallback
Data_sort=[]


def last_boxed_only_string(string):
    idx = string.rfind("\\boxed")
    if "\\boxed " in string:
        return "\\boxed " + string.split("\\boxed ")[-1].split("$")[0]
    if idx < 0:
        idx = string.rfind("\\fbox")
        if idx < 0:
            return None

    i = idx
    right_brace_idx = None
    num_left_braces_open = 0
    while i < len(string):
        if string[i] == "{":
            num_left_braces_open += 1
        if string[i] == "}":
            num_left_braces_open -= 1
            if num_left_braces_open == 0:
                right_brace_idx = i
                break
        i += 1

    if right_brace_idx is None:
        retval = None
    else:
        retval = string[idx:right_brace_idx + 1]

    return retval

def remove_boxed(s):
    if s is None:
        return ""
    if "\\boxed " in s:
        left = "\\boxed "
        assert s[:len(left)] == left
        return s[len(left):]

    left = "\\boxed{"

    # assert s[:len(left)] == left
    # assert s[-1] == "}"

    return s[len(left):-1]
    

def safe_float_equal(a, b, tol=1e-5):
    try:
        return float(a)==float(b)
    except (ValueError, TypeError):
        return False


def extract_xml_answer(solution_str: str) -> str:
    string_in_last_boxed = last_boxed_only_string(solution_str)
    if string_in_last_boxed is not None:
        return remove_boxed(string_in_last_boxed)
    else:
        return None
        



def fix_a_slash_b(string):
    if len(string.split("/")) != 2:
        return string
    a = string.split("/")[0]
    b = string.split("/")[1]
    try:
        a = int(a)
        b = int(b)
        assert string == "{}/{}".format(a, b)
        new_string = "\\frac{" + str(a) + "}{" + str(b) + "}"
        return new_string
    except AssertionError:
        return string

def fix_fracs(string):
    substrs = string.split("\\frac")
    new_str = substrs[0]
    if len(substrs) > 1:
        substrs = substrs[1:]
        for substr in substrs:
            new_str += "\\frac"
            if substr[0] == "{":
                new_str += substr
            else:
                try:
                    assert len(substr) >= 2
                except AssertionError:
                    return string
                a = substr[0]
                b = substr[1]
                if b != "{":
                    if len(substr) > 2:
                        post_substr = substr[2:]
                        new_str += "{" + a + "}{" + b + "}" + post_substr
                    else:
                        new_str += "{" + a + "}{" + b + "}"
                else:
                    if len(substr) > 2:
                        post_substr = substr[2:]
                        new_str += "{" + a + "}" + b + post_substr
                    else:
                        new_str += "{" + a + "}" + b
    string = new_str
    return string




def strip_string(string):
    # linebreaks
    string = string.replace("\n", "")

    # remove inverse spaces
    string = string.replace("\\!", "")

    # replace \\ with \
    string = string.replace("\\\\", "\\")

    # replace tfrac and dfrac with frac
    string = string.replace("tfrac", "frac")
    string = string.replace("dfrac", "frac")

    # remove \left and \right
    string = string.replace("\\left", "")
    string = string.replace("\\right", "")

    # Remove circ (degrees)
    string = string.replace("^{\\circ}", "")
    string = string.replace("^\\circ", "")

    # remove dollar signs
    string = string.replace("\\$", "")

    # remove units (on the right)
    string = remove_right_units(string)

    # remove percentage
    string = string.replace("\\%", "")
    string = string.replace("\%", "")  # noqa: W605

    # " 0." equivalent to " ." and "{0." equivalent to "{." Alternatively, add "0" if "." is the start of the string
    string = string.replace(" .", " 0.")
    string = string.replace("{.", "{0.")
    # if empty, return empty string
    if len(string) == 0:
        return string
    if string[0] == ".":
        string = "0" + string

    # to consider: get rid of e.g. "k = " or "q = " at beginning
    if len(string.split("=")) == 2:
        if len(string.split("=")[0]) <= 2:
            string = string.split("=")[1]

    # fix sqrt3 --> sqrt{3}
    string = fix_sqrt(string)

    # remove spaces
    string = string.replace(" ", "")

    # \frac1b or \frac12 --> \frac{1}{b} and \frac{1}{2}, etc. Even works with \frac1{72} (but not \frac{72}1). Also does a/b --> \\frac{a}{b}
    string = fix_fracs(string)

    # manually change 0.5 --> \frac{1}{2}
    if string == "0.5":
        string = "\\frac{1}{2}"

    # NOTE: X/Y changed to \frac{X}{Y} in dataset, but in simple cases fix in case the model output is X/Y
    string = fix_a_slash_b(string)

    return string



def is_equiv(str1, str2, verbose=False):
    if str1 is None and str2 is None:
        print("WARNING: Both None")
        return True
    if str1 is None or str2 is None:
        return False

    try:
        ss1 = strip_string(str1)
        ss2 = strip_string(str2)
        if verbose:
            print(ss1, ss2)
        return ss1 == ss2
    except Exception:
        return str1 == str2



def correctness_reward_func(prompts, completions, Angle_metric, answer, **kwargs) -> list[float]:
    """Reward function that checks if the answer is correct."""
    responses = [completion[0]['content'] for completion in completions]
    extracted_responses = [extract_xml_answer(r) for r in responses]
    print(f"Question: {prompts[0][-1]['content']}\nAnswer: {answer[0]}\nResponse: {responses[0]}\nExtracted: {extracted_responses[0]}")
 
    Angle_metric_average = [sum(Angle_metric[i:i+4]) / 4 for i in range(0, len(Angle_metric), 4)]
    correct = [2.0 if is_equiv(r, a) else 0.0 for r, a in zip(extracted_responses, answer)]
    correct_average = [sum(correct[i:i+4]) / 4 for i in range(0, len(correct), 4)]
    data_current = [{"accuracy": a, "angle": b} for a, b in zip(correct_average, Angle_metric_average)]
    Data_sort.extend(data_current)
    
    print(''.join('✅' if is_equiv(r, a) else '❌' for r, a in zip(extracted_responses, answer)))
    return [2.0 if is_equiv(r, a) else 0.0 for r, a in zip(extracted_responses, answer)]






# GAINRL Sampler

def gaussian_sample_list(A, num_samples, center_index, std_dev):
    A_len = len(A)
    indices = np.arange(A_len)
    probs = np.exp(-0.5 * ((indices - center_index) / std_dev) ** 2)
    probs /= probs.sum()
    sample_indices = np.random.choice(indices, size=min(num_samples, A_len), replace=False, p=probs)
    sample_indices.sort()
    B = [A[i] for i in sample_indices]
    return B



def update_dataset(sort_list, mean, std, subset_size, loop):
    if loop == 0:
        mean_new = mean
    else:
        global Data_sort
        avg_accuracy_now = sum(d["accuracy"] for d in Data_sort) / len(Data_sort)
        avg_angle_now = sum(d["angle"] for d in Data_sort) / len(Data_sort)
        device = torch.device("cuda")
        acc = torch.tensor(avg_accuracy_now, dtype=torch.float32, device=device)
        ang = torch.tensor(avg_angle_now, dtype=torch.float32, device=device)
        adjustment = 500 * torch.tanh(2 * (acc/2 - 0.5)) + 500 * torch.tanh(2 * ang)
        adjustment = torch.clamp(adjustment, 0, 1000)
        
        mean_new = mean + adjustment.item()
        print("mean_new:")
        print(mean_new)
    
    new_index = gaussian_sample_list(sort_list, num_samples=subset_size, center_index=mean, std_dev=std)
    Data_sort=[]
    return new_index, mean_new


from torch.utils.data import Sampler

class GainRLSampler(Sampler):
    def __init__(self, indices):
        self.indices = list(indices)

    def __iter__(self):
        return iter(self.indices)

    def __len__(self):
        return len(self.indices)
class DatasetUpdateCallback(TrainerCallback):
    def __init__(self, sort_list, total_loops, subset_size):
        super().__init__()
        self.sort_list = sort_list                    
        self.total_loops = total_loops                
        self.subset_size = subset_size                
        self.loop = 0                                 
        self.mean = 0
        self.std = 1000
        self.drop_list = []
        

    def _next_subset(self):
        new_indices, self.mean = update_dataset(self.sort_list,self.mean, self.std, self.subset_size, self.loop)
        
        return new_indices

     
    def on_epoch_begin(self, args, state, control, train_dataloader=None, **kwargs):
        if self.loop >= self.total_loops - 1:           
            return control

        if train_dataloader is None:                    
            return control

        new_indices = self._next_subset()
        print('Sample Data Index:')
        print(new_indices)
        new_sampler = GainRLSampler(new_indices)
        train_dataloader.base_dataloader.batch_sampler.sampler = new_sampler
        
        self.loop += 1
        return control



def main():
    args = parse_args()

     
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_id
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:128"

    
    model_tag   = Path(args.model_name).name.replace("/", "_")
    output_dir  = args.output_dir or \
        f"trl_gainrl/outputs/{args.dataset}/{model_tag}-GainRL"

     
    with open(args.dataset_path, encoding="utf-8") as f:
        raw_data = json.load(f)
    hf_dataset = Dataset.from_list(raw_data)
    dataset    = preprocess_dataset(hf_dataset, chunk_size=500)

    correct_list = torch.load(args.indices_path).tolist()

     
    cfg = GRPOConfig(
        output_dir=output_dir,
        run_name=model_tag,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        num_train_epochs=args.num_epochs,
        max_prompt_length=args.max_prompt_len,
        max_completion_length=args.max_completion_len,
        num_generations=args.num_generations, 
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        save_steps=args.save_steps,
        logging_steps=args.logging_steps,
        beta=0.005,
        optim="adamw_8bit",
        adam_beta1=0.9,
        adam_beta2=0.99,
        weight_decay=0.1,
        warmup_ratio=0.0,
        lr_scheduler_type="constant",
        bf16=True,
        max_grad_norm=0.1,
        report_to="wandb",
        log_on_each_node=False,
        use_vllm=True,
        vllm_init_kwargs={
            "device": f"cuda:{args.gpu_id}",
            "gpu_memory_utilization": 0.3,
            "max_model_len": args.max_prompt_len + args.max_completion_len,
            "dtype": "half",
        },
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        logit_computation_mini_batch_size=1,
        enable_profiling=False,
    )

    
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name, torch_dtype=torch.bfloat16, device_map="auto")
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name, model_max_length=cfg.max_completion_length)
    tokenizer.pad_token = tokenizer.eos_token

    
    dataset_cb = DatasetUpdateCallback(
        sort_list=correct_list, total_loops=args.total_loops,
        subset_size=args.subset_size)

    trainer = GRPOTrainer(
        model=model,
        processing_class=tokenizer,
        reward_funcs=[correctness_reward_func],
        args=cfg,
        train_dataset=dataset,
        callbacks=[dataset_cb],
    )
    trainer.train()


if __name__ == "__main__":
    main()
