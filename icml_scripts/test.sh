#!/bin/bash

CUDA_VISIBLE_DEVICES=0,1 python3 zmq_train.py --seed 0 --env-set train \
    --n-house 3 --n-proc 3 --batch-size 3 --t-max 2 --max-episode-len 5 \
    --hardness 0.95 --reward-type delta --success-measure see \
    --multi-target --use-target-gating --include-object-target \
    --segmentation-input color --depth-input --resolution normal \
    --render-gpu 0,1 --max-iters 100000 \
    --algo a3c --lrate 0.001 --weight-decay 0.00001 --gamma 0.95 --batch-norm \
    --entropy-penalty 0.1 --q-loss-coef 1.0 --grad-clip 1.0 \
    --rnn-units 256 --rnn-layers 1 --rnn-cell lstm \
    --report-rate 20 --save-rate 1000 --eval-rate 200000 \
    --save-dir ./_model_/tmp \
    --log-dir ./log/tmp

