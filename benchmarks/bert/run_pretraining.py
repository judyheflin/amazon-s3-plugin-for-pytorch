# coding=utf-8
# Copyright (c) 2019 NVIDIA CORPORATION. All rights reserved.
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# ==================
import csv
import os
import time
import logging
import argparse
import random
import h5py
from tqdm import tqdm, trange
import numpy as np
import torch
from torch.utils.data import IterableDataset, DataLoader
import math
from apex import amp
import multiprocessing

from tokenization import BertTokenizer
from modeling import BertForPreTraining, BertConfig
from optimization import BertLAMB

from file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from utils import is_main_process
from apex.parallel import DistributedDataParallel as DDP
from schedulers import LinearWarmUpScheduler
from apex.parallel.distributed import flat_dist_call
import amp_C
import apex_C
from apex.amp import _amp_state

from concurrent.futures import ProcessPoolExecutor

# packages for s3 plugin example
from awsio.python.lib.io.s3.s3dataset import S3IterableDataset
import io
import warnings
warnings.filterwarnings("ignore")


logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


def create_data_samples_from_file(fileobj):

    keys = ['input_ids', 'input_mask', 'segment_ids', 'masked_lm_positions', 'masked_lm_ids', 'next_sentence_labels']
    dataset = io.BytesIO(fileobj)

    data_file = []
    with h5py.File(dataset, "r") as f:
        data_file = [np.asarray(f[key][:]) for key in keys]

    return data_file

def format_sample(sample, max_pred_length):
        [input_ids, input_mask, segment_ids, masked_lm_positions, masked_lm_ids, next_sentence_labels] = [
            torch.from_numpy(input.astype(np.int64)) if indice < 5 else torch.from_numpy(
                np.asarray(input.astype(np.int64))) for indice, input in enumerate(sample)]

        masked_lm_labels = torch.ones(input_ids.shape, dtype=torch.long) * -1
        index = max_pred_length
        # store number of  masked tokens in index
        padded_mask_indices = (masked_lm_positions == 0).nonzero()
        if len(padded_mask_indices) != 0:
            index = padded_mask_indices[0].item()
        masked_lm_labels[masked_lm_positions[:index]] = masked_lm_ids[:index]

        return [input_ids, segment_ids, input_mask,
                masked_lm_labels, next_sentence_labels]

class s3_dataset(IterableDataset):

    def __init__(self, phase2, max_pred_length):
        self.s3_directory = "s3://choidong-bert/phase1/training/wiki_books_corpus_training"
        if phase2:
            self.s3_directory = "s3://choidong-bert/phase2/training/wiki_books_corpus_training"
        self.max_pred_length = max_pred_length

    def data_generator(self):
        try:
            while True:
                filename, fileobj = next(self.dataset_iter)
                data_samples = create_data_samples_from_file(fileobj)
                data_sample_transpose = list(zip(*data_samples))
                random.shuffle(data_sample_transpose)
                # truncating data to run 1 epoch in 2 hours
                truncated_idx = len(data_sample_transpose) // 1000
                data_sample_transpose = data_sample_transpose[:truncated_idx]
                print(f"rank : {torch.distributed.get_rank()}, filename : {filename}")
                for sample in data_sample_transpose:
                    formatted_sample = format_sample(sample, self.max_pred_length)
                    yield formatted_sample

        except StopIteration as e:
            raise e

    def __iter__(self):
        self.dataset = S3IterableDataset(self.s3_directory, shuffle_urls=True)
        self.dataset_iter = iter(self.dataset)
        return self.data_generator()

def parse_arguments():

    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--input_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain .hdf5 files  for the task.")

    parser.add_argument("--config_file",
                        default=None,
                        type=str,
                        required=True,
                        help="The BERT model config")

    parser.add_argument("--bert_model", default="bert-large-uncased", type=str,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")

    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--max_seq_length",
                        default=512,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--max_predictions_per_seq",
                        default=80,
                        type=int,
                        help="The maximum total of masked tokens in input sequence")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=1.0, # changed to 1 for speed
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps",
                        default=1000,
                        type=float,
                        help="Total number of training steps to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.01,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumualte before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0.0,
                        help='Loss scaling, positive power of 2 values can improve fp16 convergence.')
    parser.add_argument('--log_freq',
                        type=float, default=50.0,
                        help='frequency of logging loss.')
    parser.add_argument('--checkpoint_activations',
                        default=False,
                        action='store_true',
                        help="Whether to use gradient checkpointing")
    parser.add_argument("--resume_from_checkpoint",
                        default=False,
                        action='store_true',
                        help="Whether to resume training from checkpoint.")
    parser.add_argument('--resume_step',
                        type=int,
                        default=-1,
                        help="Step to resume training from.")
    parser.add_argument('--num_steps_per_checkpoint',
                        type=int,
                        default=100,
                        help="Number of update steps until a model checkpoint is saved to disk.")
    parser.add_argument('--phase2',
                        default=False,
                        action='store_true',
                        help="Whether to train with seq len 512")
    parser.add_argument('--allreduce_post_accumulation',
                        default=False,
                        action='store_true',
                        help="Whether to do allreduces during gradient accumulation steps.")
    parser.add_argument('--allreduce_post_accumulation_fp16',
                        default=False,
                        action='store_true',
                        help="Whether to do fp16 allreduce post accumulation.")
    parser.add_argument('--accumulate_into_fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use fp16 gradient accumulators.")
    parser.add_argument('--phase1_end_step',
                        type=int,
                        default=7038,
                        help="Number of training steps in Phase1 - seq len 128")
    parser.add_argument("--do_train",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    args = parser.parse_args()
    return args

def setup_training(args):

    assert (torch.cuda.is_available())

    if args.local_rank == -1:
        device = torch.device("cuda")
        args.n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        args.n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl', init_method='env://')

    logger.info("device %s n_gpu %d distributed training %r", device, args.n_gpu, bool(args.local_rank != -1))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))
    if args.train_batch_size % args.gradient_accumulation_steps != 0:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, batch size {} should be divisible".format(
            args.gradient_accumulation_steps, args.train_batch_size))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    if not args.do_train:
        raise ValueError(" `do_train`  must be True.")

    if not args.resume_from_checkpoint and os.path.exists(args.output_dir) and (
            os.listdir(args.output_dir) and os.listdir(args.output_dir) != ['logfile.txt']):
        raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))

    if not args.resume_from_checkpoint:
        os.makedirs(args.output_dir, exist_ok=True)

    return device, args

def prepare_model_and_optimizer(args, device):

    # Prepare model
    config = BertConfig.from_json_file(args.config_file)

    # Padding for divisibility by 8
    if config.vocab_size % 8 != 0:
        config.vocab_size += 8 - (config.vocab_size % 8)
    model = BertForPreTraining(config)

    checkpoint = None
    if not args.resume_from_checkpoint:
        global_step = 0
    else:
        if args.resume_step == -1:
            model_names = [f for f in os.listdir(args.output_dir) if f.endswith(".pt")]
            args.resume_step = max([int(x.split('.pt')[0].split('_')[1].strip()) for x in model_names])
        global_step = args.resume_step

        checkpoint = torch.load(os.path.join(args.output_dir, "ckpt_{}.pt".format(global_step)), map_location="cpu")
        model.load_state_dict(checkpoint['model'], strict=False)
        if args.phase2:
            global_step -= args.phase1_end_step
        if is_main_process():
            print("resume step from ", args.resume_step)

    model.to(device)
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta', 'LayerNorm']
    
    optimizer_grouped_parameters = []
    names = []

    count = 1
    for n, p in param_optimizer:
        count += 1
        if not any(nd in n for nd in no_decay):
            optimizer_grouped_parameters.append({'params': [p], 'weight_decay': 0.01, 'name': n})
            names.append({'params': [n], 'weight_decay': 0.01})
        if any(nd in n for nd in no_decay):
            optimizer_grouped_parameters.append({'params': [p], 'weight_decay': 0.00, 'name': n})
            names.append({'params': [n], 'weight_decay': 0.00})

    optimizer = BertLAMB(optimizer_grouped_parameters,
                         lr=args.learning_rate,
                         warmup=args.warmup_proportion,
                         t_total=args.max_steps)
    if args.fp16:

        if args.loss_scale == 0:
            # optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
            model, optimizer = amp.initialize(model, optimizer, opt_level="O2", loss_scale="dynamic", 
                    master_weights=False if args.accumulate_into_fp16 else True)
        else:
            # optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)
            model, optimizer = amp.initialize(model, optimizer, opt_level="O2", loss_scale=args.loss_scale,
                    master_weights=False if args.accumulate_into_fp16 else True)
        amp._amp_state.loss_scalers[0]._loss_scale = 2**20

    if args.resume_from_checkpoint:
        if args.phase2:
            keys = list(checkpoint['optimizer']['state'].keys())
            #Override hyperparameters from Phase 1
            for key in keys:
                checkpoint['optimizer']['state'][key]['step'] = global_step
            for iter, item in enumerate(checkpoint['optimizer']['param_groups']):
                checkpoint['optimizer']['param_groups'][iter]['t_total'] = args.max_steps
                checkpoint['optimizer']['param_groups'][iter]['warmup'] = args.warmup_proportion
                checkpoint['optimizer']['param_groups'][iter]['lr'] = args.learning_rate
        optimizer.load_state_dict(checkpoint['optimizer'])  # , strict=False)

        # Restore AMP master parameters          
        if args.fp16:
            optimizer._lazy_init_maybe_master_weights()
            optimizer._amp_stash.lazy_init_called = True
            optimizer.load_state_dict(checkpoint['optimizer'])
            for param, saved_param in zip(amp.master_params(optimizer), checkpoint['master params']):
                param.data.copy_(saved_param.data)

    if args.local_rank != -1:
        if not args.allreduce_post_accumulation:
            model = DDP(model, message_size=250000000, gradient_predivide_factor=torch.distributed.get_world_size())
        else:
            flat_dist_call([param.data for param in model.parameters()], torch.distributed.broadcast, (0,) )
    elif args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    return model, optimizer, checkpoint, global_step

def take_optimizer_step(args, optimizer, model, overflow_buf, global_step):

    if args.allreduce_post_accumulation:
        # manually allreduce gradients after all accumulation steps
        # check for Inf/NaN
        # 1. allocate an uninitialized buffer for flattened gradient
        scaler = _amp_state.loss_scalers[0]
        master_grads = [p.grad for p in amp.master_params(optimizer) if p.grad is not None]
        flat_grad_size = sum(p.numel() for p in master_grads)
        allreduce_dtype = torch.float16 if args.allreduce_post_accumulation_fp16 else torch.float32
        flat_raw = torch.empty(flat_grad_size, device='cuda', dtype=allreduce_dtype)
        # 2. combine unflattening and predivision of unscaled 'raw' gradient
        allreduced_views = apex_C.unflatten(flat_raw, master_grads)
        overflow_buf.zero_()
        amp_C.multi_tensor_scale(65536,
            overflow_buf,
            [master_grads, allreduced_views],
            scaler.loss_scale() / (torch.distributed.get_world_size() * args.gradient_accumulation_steps))
        # 3. sum gradient across ranks. Because of the predivision, this averages the gradient
        torch.distributed.all_reduce(flat_raw)
        # 4. combine unscaling and unflattening of allreduced gradient
        overflow_buf.zero_()
        amp_C.multi_tensor_scale(65536,
            overflow_buf,
            [allreduced_views, master_grads],
            1./scaler.loss_scale())
        # 5. update loss scale
        scaler = _amp_state.loss_scalers[0]
        old_overflow_buf = scaler._overflow_buf
        scaler._overflow_buf = overflow_buf
        had_overflow = scaler.update_scale()
        scaler._overfloat_buf = old_overflow_buf
        # 6. call optimizer step function
        if had_overflow == 0:
            optimizer.step()
            global_step += 1
        else:
            # Overflow detected, print message and clear gradients
            if is_main_process():
                print(("Rank {} :: Gradient overflow.  Skipping step, "  +
                        "reducing loss scale to {}").format(
                        torch.distributed.get_rank(),
                        scaler.loss_scale()))
            if _amp_state.opt_properties.master_weights:
                for param in optimizer._amp_stash.all_fp32_from_fp16_params:
                    param.grad = None
        for param in model.parameters():
            param.grad = None
    else:
        optimizer.step()
        #optimizer.zero_grad()
        for param in model.parameters():
            param.grad = None
        global_step += 1

    return global_step

def main():

    args = parse_arguments()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    device, args = setup_training(args)

    # Prepare optimizer
    model, optimizer, checkpoint, global_step = prepare_model_and_optimizer(args, device)

    if is_main_process():
        print("SEED {}".format(args.seed))

    if args.do_train:
        if is_main_process():
            logger.info("***** Running training *****")
            # logger.info("  Num examples = %d", len(train_data))
            logger.info("  Batch size = %d", args.train_batch_size)
            print("  LR = ", args.learning_rate)
            print("Training. . .")

        model.train()
        average_loss = 0.0  # averaged loss every args.log_freq steps
        epoch = 0
        training_steps = 0

        overflow_buf = None
        if args.allreduce_post_accumulation:
            overflow_buf = torch.cuda.IntTensor([0])

        # Note: We loop infinitely over epochs, termination is handled via iteration count
        while epoch < args.num_train_epochs:
            logger.info("EPOCH NUMBER %s" % epoch)
            train_dataset = s3_dataset(phase2=args.phase2, max_pred_length=args.max_predictions_per_seq)
            train_dataloader = DataLoader(train_dataset, pin_memory=True)
            train_iter = tqdm(train_dataloader, desc="Iteration") if is_main_process() else train_dataloader
            for step, sample in enumerate(train_iter):
                training_steps += 1
                sample = [elem.to(device) for elem in sample]
                input_ids, segment_ids, input_mask, masked_lm_labels, next_sentence_labels = sample
                loss = model(input_ids=input_ids, token_type_ids=segment_ids, attention_mask=input_mask,
                            masked_lm_labels=masked_lm_labels, next_sentence_label=next_sentence_labels,
                            checkpoint_activations=args.checkpoint_activations)
                if args.n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.

                divisor = args.gradient_accumulation_steps
                if args.gradient_accumulation_steps > 1:
                    if not args.allreduce_post_accumulation:
                        # this division was merged into predivision
                        loss = loss / args.gradient_accumulation_steps
                        divisor = 1.0
                if args.fp16:
                    with amp.scale_loss(loss, optimizer, delay_overflow_check=args.allreduce_post_accumulation) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()
                average_loss += loss.item()

                if training_steps % args.gradient_accumulation_steps == 0:
                    global_step = take_optimizer_step(args, optimizer, model, overflow_buf, global_step)

                if training_steps % (args.log_freq * args.gradient_accumulation_steps) == 0:
                    if is_main_process():
                        print("Step:{} Average Loss = {} Step Loss = {} LR {}".format(global_step, average_loss / (
                                    args.log_freq * divisor), loss.item() * args.gradient_accumulation_steps / divisor,
                                    optimizer.param_groups[0]['lr']))
                    average_loss = 0

            epoch += 1

    return args


if __name__ == "__main__":
    now = time.time()
    args = main()
    if is_main_process():
        print("Total time taken {}".format(time.time() - now))