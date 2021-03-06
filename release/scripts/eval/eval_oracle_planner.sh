#!/usr/bin/env bash
MODEL_DIR="./release/metadata/motion_dict.json"
SEMANTIC_DIR="./release/metadata/semantic_oracle_rooms.json"

noise="0.85"
all_ep_len="300 1000"
all_exp_len="10"

seed=7
max_iters=6000

TERM="mask"

required="4:253,5:436"

for exp_len in $all_exp_len
do
    for ep_len in $all_ep_len
    do
         CUDA_VISIBLE_DEVICES=2 python3 HRL/eval_HRL.py --seed $seed --env-set test --house -50 \
            --hardness 0.95 --render-gpu 1 --max-birthplace-steps 40 --min-birthplace-grids 1 \
            --planner oracle \
            --success-measure see --multi-target --use-target-gating --terminate-measure $TERM \
            --only-eval-room-target \
            --planner-obs-noise $noise \
            --motion mixture --mixture-motion-dict $MODEL_DIR \
            --max-episode-len $ep_len --n-exp-steps $exp_len --max-iters $max_iters \
            --segmentation-input color --depth-input \
            --rnn-units 256 --rnn-layers 1 --rnn-cell lstm --batch-norm \
            --store-history \
            --log-dir ./results/oracle_planner_main \
            --backup-rate 1000

        # additional episodes for faraway targets 
        CUDA_VISIBLE_DEVICES=2 python3 HRL/eval_HRL.py --seed 7000 --env-set test --house -50 \
            --hardness 0.95 --render-gpu 1 --max-birthplace-steps 40 --min-birthplace-grids 1 \
            --planner oracle \
            --success-measure see --multi-target --use-target-gating --terminate-measure $TERM \
            --only-eval-room-target \
            --planner-obs-noise $noise \
            --motion mixture --mixture-motion-dict $MODEL_DIR \
            --max-episode-len $ep_len --n-exp-steps $exp_len --plan-dist-iters $required \
            --segmentation-input color --depth-input \
            --rnn-units 256 --rnn-layers 1 --rnn-cell lstm --batch-norm \
            --store-history \
            --log-dir ./results/oracle_planner_add
   
    done
done

