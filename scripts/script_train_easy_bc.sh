#!/bin/bash

CUDA_VISIBLE_DEVICES=1 python train.py --seed 0 --update-freq 25 --linear-reward \
    --lrate 0.0001 --critic-lrate 0.001 --gamma 0.95 \
    --save-dir ./_model_/new_env/linear_reward/easy/bc_lr1e4_1e3_sftmx_d \
    --log-dir ./log/new_env/linear_reward/easy/bc_lr1e4_1e3_sftmx_d \
    --batch-size 256 --hardness 0.3 --batch-norm --entropy-penalty 0.001 --no-debug
