# -*-coding=utf-8 -*-

import argparse
import math
import os
import random
import time
import logging

import numpy as np
import torch.nn.functional as F
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data import DataLoader 
import torch
from torch.cuda import amp

from models.yolo_fasteset import YoloFastest
from dataset.voc_dataset import SimpleDataset
from loss.detection_loss import compute_loss

logger = logging.getLogger(__name__)



def train(params, device):

    # cudnn benchmark
    import torch.backends.cudnn as cudnn
    cudnn.deterministic = False
    cudnn.benchmark = True
    
    save_path = params["io_params"]["save_path"]
    train_path = params["io_params"]["train_path"]
    input_size = params["io_params"]["input_size"]
    total_epochs = params["train_params"]["total_epochs"]
    batch_size = params["train_params"]["batch_size"]

    model = YoloFastest(params["io_params"]).to(device)
    
    dataset = SimpleDataset(train_path, input_size, augment=True, 
                            aug_params=params["augment_params"], stride=32) 
    dataloader = DataLoader(dataset, batch_size=batch_size, num_workers=8, sampler=None,
                            pin_memory=True, collate_fn=SimpleDataset.collate_fn)
    
    batch_per_epoch = len(dataloader)
    num_warm = max(3*batch_per_epoch, 1e3)

    nbs = 64  # nominal batch size
    accumulate = max(round(nbs / batch_size), 1)  # accumulate loss before optimizing
    # hyp['weight_decay'] *= batch_size * accumulate / nbs  # scale weight_decay


    train_params = params["train_params"]
    optimizer = optim.Adam(model.parameters(), lr=train_params['lr0'], betas=(train_params['momentum'], 0.999))
    lf = lambda x: (((1 + math.cos(x * math.pi / total_epochs)) / 2) ** 1.0) * 0.8 + 0.2
    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)

    
    # Check anchors
    # check_anchors(dataset, model=model, thr=hyp['anchor_t'], imgsz=imgsz)
    

    t0 = time.time()
    
    start_epoch = 0
    scheduler.last_epoch = start_epoch - 1
    scaler = amp.GradScaler(enabled=True)

    for epoch in range(start_epoch, total_epochs): 
        model.train()
        optimizer.zero_grad()

        mloss = torch.zeros(4, device=device)  # mean losses for each epoch
        for batch_id, (imgs, targets) in enumerate(dataloader):
            num_iter = batch_id + batch_per_epoch * epoch  # 训练的总迭代次数
            
            imgs = imgs.to(device, non_blocking=True).float() / 255.0

            # Warmup
            if num_iter <= num_warm:
                xi = [0, num_warm]

                accumulate = max(1, np.interp(num_iter, xi, [1, nbs / batch_size]).round())
                for j, x in enumerate(optimizer.param_groups):
                    x['lr'] = np.interp(num_iter, xi, [0.1 if j == 2 else 0.0, x['initial_lr'] * lf(epoch)])
                    if 'momentum' in x:
                        x['momentum'] = np.interp(num_iter, xi, [0.9, hyp['momentum']])

            # Autocast
            with amp.autocast(enabled=True):
                pred = model(imgs)
                loss, loss_items = compute_loss(pred, targets.to(device), params["io_params"]["anchors"])
            
            scaler.scale(loss).backward()

            # Optimize
            if num_iter % accumulate == 0:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()

                mloss = (mloss * batch_id + loss_items) / (batch_id + 1)  # update mean losses
            
            print("loss: ", loss.item(),  loss_items)

        # Scheduler
        lr = [x['lr'] for x in optimizer.param_groups]
        scheduler.step()
        
        # Save model
        ckpt = {'epoch': epoch,
                'model': model.state_dict(),
                'optimizer': None if epoch+1==total_epochs else optimizer.state_dict()}
        torch.save(ckpt, save_path+"/epoch_"+str(epoch)+'.pt')
        del ckpt

    logger.info('%g epochs completed in %.3f minutes.\n' % (epoch - start_epoch + 1, (time.time() - t0) / 60))

    torch.cuda.empty_cache()
    

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rect', action='store_true', help='rectangular training') # 默认为False
    parser.add_argument('--resume', nargs='?', const=True, default=False, help='resume most recent training')
    parser.add_argument('--noautoanchor', action='store_true', help='disable autoanchor check')
    parser.add_argument('--evolve', action='store_true', help='evolve hyperparameters')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--multi-scale', action='store_true', help='vary img-size +/- 50%%')
    parser.add_argument('--adam', action='store_true', help='use torch.optim.Adam() optimizer')
    opt = parser.parse_args()
   
    device = torch.device('cuda:0')
    
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    logger.info(opt)

    
    params = {
        "io_params": {
            "save_path" : 'output',
            "train_path" : '/home/lance/data/DataSets/quanzhou/coco_style/mini',
            "input_size" : 640,
            "num_cls" :  1,
            "anchors" :  [[[12, 18],  [37, 49],  [52,132]], 
                          [[115, 73], [119,199], [242,238]]],
            },

        "augment_params" :{
            "hsv_h": 0.015,    # image HSV-Hue augmentation (fraction)
            "hsv_s": 0.7,      # image HSV-Saturation augmentation (fraction)
            "hsv_v": 0.4,      # image HSV-Value augmentation (fraction)
            "degrees": 0.0,    # image rotation (+/- deg)
            "translate": 0.1,  # image translation (+/- fraction)
            "scale": 0.5,      # image scale (+/- gain)
            "shear": 0.0,      # image shear (+/- deg)
            "perspective": 0.0,  # image perspective (+/- fraction), range 0-0.001
            "flipud": 0.0,     # image flip up-down (probability)
            "fliplr": 0.5,     # image flip left-right (probability)
            "mixup": 0.0,      # image mixup (probability)
            },

        "train_params" : {
            "total_epochs" : 100,
            "batch_size" : 32,
            "lr0": 0.01,         # initial learning rate (SGD=1E-2, Adam=1E-3)
            "momentum": 0.937,   # SGD momentum/Adam beta1
            "weight_decay": 0.0005, 
            },
        
        # "network_params" : {}        
    }
    
    train(params, device)
