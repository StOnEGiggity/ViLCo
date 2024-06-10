import os
import pprint
import random
import numpy as np
import torch
import torch.nn.parallel
import torch.optim
import itertools
import argparse

import torch.utils.data
import torch.utils.data.distributed
import torch.distributed as dist
from torch.cuda.amp import autocast as autocast

from config.config import config, update_config

from model.corr_clip_spatial_transformer2_anchor_2heads_hnm import ClipMatcher
from utils import exp_utils, train_utils, dist_utils
from dataset import dataset_utils
from dataset.cl_benchmark import QILSetTask
from func.train_anchor import train_epoch, validate_cl, final_validate
from cl_methods import on_task_update, on_task_mas_update

import transformers
import pickle


def load_best_checkpoint(model, file_folder, file_name, current_task, gpu_id):
    path_best_model = os.path.join(file_folder, file_name)
    if os.path.exists(path_best_model):
        checkpoint_dict = torch.load(path_best_model, map_location=lambda storage, loc: storage.cuda(gpu_id))
        model.module.load_state_dict(checkpoint_dict['state_dict'])
        model.module.reg_params = checkpoint_dict['reg_params']
        model = model.cuda()
    return model

def parse_args():
    parser = argparse.ArgumentParser(description='Train hand reconstruction network')
    parser.add_argument(
        '--cfg', help='experiment configure file name', required=True, type=str)
    parser.add_argument(
        "--eval", dest="eval", action="store_true",help="evaluate model")
    parser.add_argument(
        '--local_rank', default=-1, type=int, help='node rank for distributed training')
    args, rest = parser.parse_known_args()
    update_config(args.cfg)
    return args


def main():
    # Get args and config
    args = parse_args()
    logger, output_dir, tb_log_dir = exp_utils.create_logger(config, args.cfg, phase='train')
    logger.info(pprint.pformat(args))
    logger.info(pprint.pformat(config))
    
    global list_val_iou_ii
    list_val_iou_ii = {'val': []}

    # set random seeds
    torch.cuda.manual_seed_all(config.seed)
    torch.manual_seed(config.seed)
    np.random.seed(config.seed)
    random.seed(config.seed)

    # set device
    gpus = range(torch.cuda.device_count())
    distributed = torch.cuda.device_count() > 1
    device = torch.device('cuda') if len(gpus) > 0 else torch.device('cpu')
    if "LOCAL_RANK" in os.environ:
        dist_utils.dist_init(int(os.environ["LOCAL_RANK"]))
    local_rank = dist.get_rank()
    torch.cuda.set_device(local_rank)

    wandb_run = None
    
    # CL benchmark
    path_data = config['cl']['pkl_file']
    with open(path_data, 'rb') as handle:
        data = pickle.load(handle)
    memory_size = config['cl']['memory_size']
    random_order = config['cl']['random_order']
    path_memory = config['cl']['path_memory']

    # get model
    model = ClipMatcher(config).to(device)
    #model = torch.compile(model)

    # get optimizer
    optimizer = train_utils.get_optimizer(config, model)
    # schedular = train_utils.get_schedular(config, optimizer)
    schedular = transformers.get_linear_schedule_with_warmup(optimizer,
                                                             num_warmup_steps=config.train.schedular_warmup_iter,
                                                             num_training_steps=config.train.total_iteration)
    scaler = torch.cuda.amp.GradScaler()

    best_iou, best_prob = 0.0, 0.0
    ep_resume = None
    if config.train.resume:
        try:
            model, optimizer, schedular, scaler, ep_resume, best_iou, best_prob = train_utils.resume_training(
                                                                                model, optimizer, schedular, scaler, 
                                                                                output_dir,
                                                                                cpt_name='cpt_last.pth.tar')
            print('LR after resume {}'.format(optimizer.param_groups[0]['lr']))
        except:
            print('Resume failed')

    # distributed training
    ddp = False
    model =  torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    if device == torch.device("cuda"):
        torch.backends.cudnn.benchmark = True
        device_ids = range(torch.cuda.device_count())
        print("using {} cuda".format(len(device_ids)))
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank], find_unused_parameters=True)
        device_num = len(device_ids)
        ddp = True

    # get dataset and dataloader    
    # train_data = dataset_utils.get_dataset(config, split='train')
    # train_sampler = torch.utils.data.distributed.DistributedSampler(train_data)
    # train_loader = torch.utils.data.DataLoader(train_data,
    #                                            batch_size=config.train.batch_size, 
    #                                            shuffle=False,
    #                                            num_workers=int(config.workers), 
    #                                            pin_memory=True, 
    #                                            drop_last=True,
    #                                            sampler=train_sampler)
    # val_data = dataset_utils.get_dataset(config, split='val')
    # val_loader = torch.utils.data.DataLoader(val_data,
    #                                            batch_size=config.test.batch_size, 
    #                                            shuffle=False,
    #                                            num_workers=int(config.workers), 
    #                                            pin_memory=True, 
    #                                            drop_last=False) 
    train_qilDatasetList = QILSetTask(config, data['train'], memory_size, shuffle=True, train_enable = True, shuffle_task_order=random_order)
    val_qilDatasetList = QILSetTask(config, data['val'], memory_size, shuffle=False, train_enable = False, shuffle_task_order=False)
    current_task = 0
    
    iter_trainDataloader = iter(train_qilDatasetList)
    num_tasks = train_qilDatasetList.num_tasks
    data, train_loader_i, num_next_queries = next(iter_trainDataloader)

    for j in range(current_task, num_tasks):
        start_ep = ep_resume if ep_resume is not None else 0
        end_ep = 1 #int(config.train.total_iteration / len(train_loader_i)) + 1
        print('end_epoch', end_ep)
        
        if j != 0:
            data, train_loader_i, num_next_queries = next(iter_trainDataloader)
        with torch.no_grad():
            best_iou, best_prob = validate_cl(config, val_qilDatasetList, model, 0, j, output_dir=output_dir, device=device, rank=local_rank, ddp=ddp, wandb_run=wandb_run)
            logger.info('Best init iou: {} Best prob {} Task: {}'.format(best_iou, best_prob, j+1))
        # train
        for epoch in range(start_ep, end_ep):
            train_loader_i.sampler.set_epoch(epoch)
            train_epoch(config,
                        loader=train_loader_i,
                        model=model,
                        optimizer=optimizer,
                        schedular=schedular,
                        scaler=scaler,
                        epoch=epoch,
                        output_dir=output_dir,
                        device=device,
                        rank=local_rank,
                        ddp=ddp,
                        wandb_run=wandb_run,
                        cl_name=config['cl']['name'], 
                        reg_lambda=config['cl']['reg_lambda'],
                        current_task_id=j
                        )
            torch.cuda.empty_cache()

            if local_rank == 0:
                train_utils.save_checkpoint(
                        {
                            'epoch': epoch + 1,
                            'state_dict': model.module.state_dict(),
                            'optimizer': optimizer.state_dict(),
                            'schedular': schedular.state_dict(),
                            'scaler': scaler.state_dict(),
                            'current_task': j,
                            'reg_params': model.module.reg_params,
                        }, 
                        checkpoint=output_dir, filename="cpt_last.pth.tar")

            if epoch % 5 == 0:
                print('Doing validation...')
                iou, prob = validate_cl(config,
                                        val_qilDatasetList,
                                        epoch=epoch,
                                        current_task_id=j,
                                        model=model,
                                        output_dir=output_dir,
                                        device=device,
                                        rank=local_rank,
                                        ddp=ddp,
                                        wandb_run=wandb_run
                                        )
                torch.cuda.empty_cache()
                if iou > best_iou:
                    best_iou = iou
                    if local_rank == 0:
                        train_utils.save_checkpoint(
                        {
                            'epoch': epoch + 1,
                            'state_dict': model.module.state_dict(),
                            'optimizer': optimizer.state_dict(),
                            'schedular': schedular.state_dict(),
                            'scaler': scaler.state_dict(),
                            'best_iou': best_iou,
                            'current_task': j,
                            'reg_params': model.module.reg_params,
                        }, 
                        checkpoint=output_dir, filename="cpt_best_iou_task_{:02d}.pth.tar".format(j))

                if prob > best_prob:
                    best_prob = prob
                    if local_rank == 0:
                        train_utils.save_checkpoint(
                        {
                            'epoch': epoch + 1,
                            'state_dict': model.module.state_dict(),
                            'optimizer': optimizer.state_dict(),
                            'schedular': schedular.state_dict(),
                            'scaler': scaler.state_dict(),
                            'best_prob': best_prob,
                        }, 
                        checkpoint=output_dir, filename="cpt_best_prob_task_{:02d}.pth.tar".format(j))

                logger.info('Rank {}, best iou: {} (current {}), best probability accuracy: {} (current {})'.format(local_rank, best_iou, iou, best_prob, prob))
            dist.barrier()
            torch.cuda.empty_cache()
        
        if memory_size != 'ALL':
            if torch.cuda.device_count() > 1:
                m = memory_size // 13
            else:
                m = memory_size // 13
        else:
            m = 'ALL'
            
        if memory_size != 0:
            model.add_samples_to_mem(val_qilDatasetList, data, m)
        train_qilDatasetList.memory = model.module.memory
        model = load_best_checkpoint(model, file_folder=output_dir, file_name="cpt_best_iou_task_{:02d}.pth.tar".format(j), current_task=j, gpu_id=device)
        
        if local_rank == 0:
            with torch.no_grad():
                iou, prob, bwf = final_validate(config,
                                                val_qilDatasetList,
                                                current_task_id=j,
                                                model=model,
                                                output_dir=output_dir,
                                                device=device,
                                                rank=local_rank,
                                                ddp=ddp,
                                                wandb_run=wandb_run,
                                                list_val_iou_ii=list_val_iou_ii
                                                )
                torch.cuda.empty_cache()
                logger.info('Task{}, Rank {}, best iou: {} (current {}), best probability accuracy: {} (current {}), BWF {}'.format(j, local_rank, best_iou, iou, best_prob, prob, bwf))

        # EWC Method
        if config['cl']['name'] == 'ewc':
            model.reg_params = on_task_update(train_loader_i, device, optimizer, model, config, j)
        elif config['cl']['name'] == 'mas':
            model.reg_params = on_task_mas_update(train_loader_i, device, optimizer, model, config, j)
            
        # get optimizer
        optimizer = train_utils.get_optimizer(config, model)
        # schedular = train_utils.get_schedular(config, optimizer)
        schedular = transformers.get_linear_schedule_with_warmup(optimizer,
                                                                num_warmup_steps=config.train.schedular_warmup_iter,
                                                                num_training_steps=config.train.total_iteration)

if __name__ == '__main__':
    main()