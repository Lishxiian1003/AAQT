# coding=utf-8
from __future__ import absolute_import, division, print_function
import logging
import argparse
import os
import random
import numpy as np

from datetime import timedelta
import time

import torch
import torch.distributed as dist

from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
from apex import amp 
from apex.parallel import DistributedDataParallel as DDP

# from models.model_INat2017 import VisionTransformer, CONFIGS
from models.model import VisionTransformer, CONFIGS

from utils.scheduler import WarmupLinearSchedule, WarmupCosineSchedule
from utils.data_utils import get_loader
from utils.dist_util import get_world_size

import torch.multiprocessing as mp
# mp.set_start_method('spawn', force=True)
mp.set_start_method('fork', force=True)

logger = logging.getLogger(__name__)


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def simple_accuracy(preds, labels):
    return (preds == labels).mean()


def save_model(args, model, optimizer, scheduler, global_step, test_best_acc):
    model_to_save = model.module if hasattr(model, 'module') else model
    model_checkpoint = os.path.join(args.output_dir, "%s_checkpoint.bin" % args.name)
    torch.save({
        'model_state_dict': model_to_save.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict': scheduler.state_dict(),
        'global_step': global_step,
        'test_best_acc': test_best_acc,
        'args': args,
    }, model_checkpoint)
    logger.info("Saved model checkpoint to [DIR: %s]", args.output_dir)


def resume_training(args, model, optimizer, scheduler):
    model_checkpoint = os.path.join(args.output_dir, "%s_checkpoint.bin" % args.name)
    if os.path.isfile(model_checkpoint):
        checkpoint = torch.load(model_checkpoint, map_location=args.device)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        args.start_step = checkpoint['global_step']
        test_best_acc = checkpoint['test_best_acc']
        logger.info("Resumed from checkpoint at step %d", args.start_step)
        return model, optimizer, scheduler, test_best_acc
    else:
        raise FileNotFoundError("No checkpoint found at {}".format(model_checkpoint))

def reduce_mean(tensor, nprocs):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= nprocs

    return rt

def setup(args):
    # Prepare model
    config = CONFIGS[args.model_type]
    
    if args.dataset == "dogs": 
        num_classes = 120
    elif args.dataset == "CUB":
        num_classes=200
    elif args.dataset == "nabirds":
        num_classes = 555
    elif args.dataset == "INat2017":
        num_classes = 5089
    else:
        raise Exception(f'Unknown dataset "{args.dataset}"')

    model = VisionTransformer(config, args.img_size, zero_head=True, num_classes=num_classes,smoothing_value=args.smoothing_value, dataset=args.dataset, \
         contr_loss=args.contr_loss, focal_loss=args.focal_loss)
    model.load_from(np.load(args.pretrained_dir))
    model.to(args.device)
    num_params = count_parameters(model)

    logger.info("{}".format(config))
    logger.info("Training parameters %s", args)
    logger.info("Total Parameter: \t%2.1fM" % num_params)

    return args, model


def count_parameters(model):
    params = sum(p.numel() for p in model.parameters() if p.requires_grad)

    return params/1000000


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def valid(args, model, writer, test_loader, global_step):
    # Validation!
    eval_losses = AverageMeter()

    logger.info("***** Running Validation *****")
    logger.info("  Num steps = %d", len(test_loader))
    logger.info("  Batch size = %d", args.eval_batch_size)

    model.eval()
    all_preds, all_label = [], []
    epoch_iterator = tqdm(test_loader,
                          desc="Validating... (loss=X.X)",
                          bar_format="{l_bar}{r_bar}",
                          dynamic_ncols=True,
                          disable=args.local_rank not in [-1, 0])
    loss_fct = torch.nn.CrossEntropyLoss()
    for step, batch in enumerate(epoch_iterator):

        if wnb: wandb.log({"step": step})

        batch = tuple(t.to(args.device) for t in batch)
        
        if args.aplly_BE:
            x, y, mask = batch
            
        else:
            x, y = batch

        with torch.no_grad():
            if args.aplly_BE:
                logits = model(x, None, mask)[0]
            else:
                logits = model(x)[0]
        
            eval_loss = loss_fct(logits, y.long()) 

            if args.contr_loss:
                eval_loss = eval_loss.mean()

            eval_losses.update(eval_loss.item())

            preds = torch.argmax(logits, dim=-1)

        if len(all_preds) == 0:
            all_preds.append(preds.detach().cpu().numpy())
            all_label.append(y.detach().cpu().numpy())
        else:
            all_preds[0] = np.append(
                all_preds[0], preds.detach().cpu().numpy(), axis=0 )
            all_label[0] = np.append(
                all_label[0], y.detach().cpu().numpy(), axis=0 )

        epoch_iterator.set_description("Validating... (loss=%2.5f)" % eval_losses.val)

    all_preds, all_label = all_preds[0], all_label[0]
    accuracy = simple_accuracy(all_preds, all_label)

    logger.info("\n")
    logger.info("Validation Results")
    logger.info("Global Steps: %d" % global_step)
    logger.info("Valid Loss: %2.5f" % eval_losses.avg)
    logger.info("Valid Accuracy: %2.5f" % accuracy)

    writer.add_scalar("test/accuracy", scalar_value=accuracy, global_step=global_step)

    if wnb: wandb.log({"acc_test": accuracy})

    return accuracy


def train(args, model, optimizer, scheduler):
    os.makedirs(args.output_dir, exist_ok=True)
    writer = SummaryWriter(log_dir=os.path.join("logs", args.name)) 
    best_step=0

    
    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    # Prepare dataset
    train_loader, test_loader = get_loader(args)

    if optimizer is None:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.learning_rate, momentum=0.9, weight_decay=args.weight_decay)
    
    if scheduler is None:
        if args.decay_type == "cosine":
            scheduler = WarmupCosineSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=args.num_steps)
        else:
            scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=args.num_steps)

    if args.fp16:
        model, optimizer = amp.initialize(models=model,
                                          optimizers=optimizer,
                                          opt_level=args.fp16_opt_level)
        amp._amp_state.loss_scalers[0]._loss_scale = 2**20

    if args.local_rank != -1:
        model = DDP(model, message_size=250000000, gradient_predivide_factor=get_world_size())

    start_time = time.time()
    logger.info("***** Running training *****")
    logger.info("  Total optimization steps = %d", args.num_steps)
    logger.info("  Instantaneous batch size per GPU = %d", args.train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                args.train_batch_size * args.gradient_accumulation_steps * (
                    torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)

    model.zero_grad()
    set_seed(args)  # Added here for reproducibility (even between python 2 and 3)
    losses = AverageMeter()
    global_step, best_acc = 0, 0

    while True:
        model.train()
        epoch_iterator = tqdm(train_loader,
                              desc="Training (X / X Steps) (loss=X.X)",
                              bar_format="{l_bar}{r_bar}",
                              dynamic_ncols=True,
                              disable=args.local_rank not in [-1, 0])

        all_preds, all_label = [], []

        for step, batch in enumerate(epoch_iterator):       
            batch = tuple(t.to(args.device) for t in batch)

            if args.aplly_BE:
                x, y, mask = batch
                loss, logits = model(x, y, mask)
            else:
                x, y = batch
                loss, logits = model(x, y)

            if args.contr_loss:
                loss = loss.mean()

            preds = torch.argmax(logits, dim=-1)

            if len(all_preds) == 0:
                all_preds.append(preds.detach().cpu().numpy())
                all_label.append(y.detach().cpu().numpy())
            else:
                all_preds[0] = np.append(
                    all_preds[0], preds.detach().cpu().numpy(), axis=0 )
                all_label[0] = np.append(
                    all_label[0], y.detach().cpu().numpy(), axis=0 )

            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps
            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            if (step + 1) % args.gradient_accumulation_steps == 0:
                losses.update(loss.item()*args.gradient_accumulation_steps)

                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()
                global_step += 1

                epoch_iterator.set_description(
                    "Training (%d / %d Steps) (loss=%2.5f)" % (global_step, args.num_steps, losses.val) )
                if args.local_rank in [-1, 0]:
                    writer.add_scalar("train/loss", scalar_value=losses.val, global_step=global_step)
                    writer.add_scalar("train/lr", scalar_value=scheduler.get_lr()[0], global_step=global_step)
                if global_step % args.eval_every == 0 and args.local_rank in [-1, 0]:
                    accuracy = valid(args, model, writer, test_loader, global_step)
                    if accuracy > best_acc:
                        save_model(args, model, optimizer, scheduler, global_step, best_acc)
                        best_acc = accuracy
                    logger.info("best accuracy so far: %f" % best_acc)
                    logger.info("best accuracy in step: %f" % global_step)
                    model.train()
                if global_step % args.num_steps == 0:
                    break

        all_preds, all_label = all_preds[0], all_label[0]
        accuracy = simple_accuracy(all_preds, all_label)
        accuracy = torch.tensor(accuracy).to(args.device)
        dist.barrier()
        train_accuracy = reduce_mean(accuracy, args.nprocs)
        train_accuracy = train_accuracy.detach().cpu().numpy()
        
        writer.add_scalar("train/accuracy", scalar_value=train_accuracy, global_step=global_step)

        if wnb: wandb.log({"acc_train": train_accuracy})

        logger.info("train accuracy so far: %f" % train_accuracy)
        logger.info("best valid accuracy in step: %f" % best_acc)
        losses.reset()
        if global_step % args.num_steps == 0:
            break

    writer.close()
    end_time = time.time()
    logger.info("Best Accuracy: \t%f" % best_acc)
    logger.info("Total Training Time: \t%f" % ((end_time - start_time) / 3600))
    logger.info("End Training!")


def main():
    parser = argparse.ArgumentParser()
    # Required parameters
    parser.add_argument("--name", required=True,
                        default="output",
                        help="Name of this run. Used for monitoring.")
    parser.add_argument("--dataset", choices=["CUB", "dogs", "nabirds","INat2017"], default="CUB",
                        help="Which downstream task.")
    parser.add_argument("--model_type", choices=["ViT-B_16", "ViT-B_32", "ViT-L_16",
                                                 "ViT-L_32", "ViT-H_14", "R50-ViT-B_16"],
                        default="ViT-B_16",
                        help="Which ViT variant to use.")
    parser.add_argument("--pretrained_dir", type=str, default="./checkpoints/imagenet21k_ViT-B_16.npz",
                        help="Where to search for pretrained ViT models.")
    parser.add_argument("--output_dir", default="output", type=str,
                        help="The output directory where checkpoints will be saved.")
    parser.add_argument("--img_size", default=400, type=int,
                        help="After-crop image resolution")
    parser.add_argument("--resize_size", default=448, type=int,
                        help="Pre-crop image resolution")
    parser.add_argument("--train_batch_size", default=4, type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", default=4, type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--eval_every", default=200, type=int,
                        help="Run prediction on validation set every so many steps."
                             "Will always run one evaluation at the end of training.")
    parser.add_argument("--num_workers", default=4, type=int,
                        help="Number of workers for dataset preparation.")
    parser.add_argument("--learning_rate", default=3e-2, type=float,
                        help="The initial learning rate for SGD.")
    parser.add_argument("--weight_decay", default=0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--num_steps", default=10000, type=int,
                        help="Total number of training steps to perform.")
    parser.add_argument("--decay_type", choices=["cosine", "linear"], default="cosine",
                        help="How to decay the learning rate.")
    parser.add_argument("--warmup_steps", default=500, type=int,
                        help="Step of training to perform learning rate warmup for.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O2',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument('--loss_scale', type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")
    parser.add_argument('--smoothing_value', type=float, default=0.0,
                        help="Label smoothing value\n")
    parser.add_argument('--aplly_BE', action='store_true',
                        help="Whether to use BE")
    parser.add_argument('--low_memory', action='store_true',
                        help="Allows to use less memory (RAM) during input image feeding. False: Slower - Do image pre-processing for the whole dataset at the beginning and store the results in memory. True: Faster - Do pre-processing on-the-go.")
    parser.add_argument('--contr_loss', action='store_true',
                        help="Whether to use contrastive loss")
    parser.add_argument('--focal_loss', action='store_true',
                        help="Whether to use focal loss")
    parser.add_argument('--data_root', type=str, default='./CUB_200_2011', # Originall
                        help="Path to the dataset\n")  
    parser.add_argument('--start_step', type=int, default=0, help="The starting step for training.")

    args = parser.parse_args()
    
    #args.data_root = '{}/{}'.format(args.data_root, args.dataset) # for future development

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl',
                                             timeout=timedelta(minutes=600))
        args.n_gpu = 1
    args.device = device
    args.nprocs = torch.cuda.device_count()

    # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s" %
                   (args.local_rank, args.device, args.n_gpu, bool(args.local_rank != -1), args.fp16))

    # Set seed
    set_seed(args)

    # Model & Tokenizer Setup
    args, model = setup(args)

    # Resume training if a checkpoint exists
    optimizer, scheduler = None, None
    if args.start_step > 0:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.learning_rate, momentum=0.9, weight_decay=args.weight_decay)
        if args.decay_type == "cosine":
            scheduler = WarmupCosineSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=args.num_steps)
        else:
            scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=args.num_steps)
        model, optimizer, scheduler, args.test_best_acc = resume_training(args, model, optimizer, scheduler)

    if wnb: wandb.watch(model)

    # Training
    train(args, model, optimizer, scheduler)


if __name__ == "__main__":
    main()
