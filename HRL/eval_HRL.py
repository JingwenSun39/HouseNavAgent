from headers import *
import common
import utils

import sys, os, platform, pickle, json, argparse, time

import numpy as np
import random

from HRL.eval_motion import create_motion
from HRL.BayesGraph import GraphPlanner, OraclePlanner, VoidPlanner
from HRL.RNNController import RNNPlanner
from HRL.semantic_oracle import SemanticOracle, OracleFunction


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)


def proc_info(info):
    return dict(yaw=info['yaw'], loc=info['loc'], grid=info['grid'],
                dist=info['dist'])


def evaluate(args, data_saver=None):

    args['segment_input'] = args['segmentation_input']

    backup_rate = args['backup_rate']

    elap = time.time()

    # Do not need to log detailed computation stats
    common.debugger = utils.FakeLogger()

    # ensure observation shape
    common.process_observation_shape('rnn', args['resolution'],
                                     args['segmentation_input'],
                                     args['depth_input'],
                                     target_mask_input=args['target_mask_input'])

    fixed_target = args['fixed_target']
    if (fixed_target is not None) and (fixed_target != 'any-room') and (fixed_target != 'any-object'):
        assert fixed_target in common.n_target_instructions, 'invalid fixed target <{}>'.format(fixed_target)

    __backup_CFG = common.CFG.copy()
    if fixed_target == 'any-room':
        common.ensure_object_targets(False)

    if args['hardness'] is not None:
        print('>>>> Hardness = {}'.format(args['hardness']))
    if args['max_birthplace_steps'] is not None:
        print('>>>> Max BirthPlace Steps = {}'.format(args['max_birthplace_steps']))
    set_seed(args['seed'])
    task = common.create_env(args['house'], task_name=args['task_name'], false_rate=args['false_rate'],
                            hardness=args['hardness'], max_birthplace_steps=args['max_birthplace_steps'],
                            success_measure=args['success_measure'],
                            depth_input=args['depth_input'],
                            target_mask_input=args['target_mask_input'],
                            segment_input=args['segmentation_input'],
                            genRoomTypeMap=False,
                            cacheAllTarget=args['multi_target'],
                            render_device=args['render_gpu'],
                            use_discrete_action=True,
                            include_object_target=args['object_target'] and (fixed_target != 'any-room'),
                            include_outdoor_target=args['outdoor_target'],
                            discrete_angle=True,
                            min_birthplace_grids=args['min_birthplace_grids'])

    if (fixed_target is not None) and (fixed_target != 'any-room') and (fixed_target != 'any-object'):
        task.reset_target(fixed_target)

    if fixed_target == 'any-room':
        common.CFG = __backup_CFG
        common.ensure_object_targets(True)
    
    # logger
    logger = utils.MyLogger(args['log_dir'], True)
    logger.print('Start Evaluating ...')


    # create semantic classifier
    if args['semantic_dir'] is not None:
        assert os.path.exists(args['semantic_dir']), '[Error] Semantic Dir <{}> not exists!'.format(args['semantic_dir'])
        assert not args['object_target'], '[ERROR] currently do not support --object-target!'
        print('Loading Semantic Oracle from dir <{}>...'.format(args['semantic_dir']))
        if args['semantic_gpu'] is None:
            args['semantic_gpu'] = common.get_gpus_for_rendering()[0]
        oracle = SemanticOracle(model_dir=args['semantic_dir'], model_device=args['semantic_gpu'], include_object=args['object_target'])
        oracle_func = OracleFunction(oracle, threshold=args['semantic_threshold'],
                                    filter_steps=args['semantic_filter_steps'], batched_size=args['semantic_batch_size'])
    else:
        oracle_func = None

    # create motion
    motion = create_motion(args, task, oracle_func=oracle_func)
    if args['motion'] == 'random':
        motion.set_skilled_rate(args['random_motion_skill'])
    flag_interrupt = args['interruptive_motion']

    # create planner
    graph = None
    max_motion_steps = args['n_exp_steps']
    if (args['planner'] == None) or (args['planner'] == 'void'):
        graph = VoidPlanner(motion)
    elif args['planner'] == 'oracle':
        graph = OraclePlanner(motion)
    elif args['planner'] == 'rnn':
        #assert False, 'Currently only support Graph-planner'
        graph = RNNPlanner(motion, args['planner_units'], args['planner_filename'], oracle_func=oracle_func)
    else:
        graph = GraphPlanner(motion)
        if not args['outdoor_target']:
            graph.add_excluded_target('outdoor')
        filename = args['planner_filename']
        if filename == 'None': filename = None
        if filename is not None:
            logger.print(' > Loading Graph from file = <{}>'.format(filename))
            with open(filename,'rb') as f:
                _params = pickle.load(f)
            graph.set_parameters(_params)
        # hack
        if args['planner_obs_noise'] is not None:
            graph.set_param(-1, args['planner_obs_noise'])  # default 0.95

    episode_success = []
    episode_good = []
    episode_stats = []
    t = 0
    seed = args['seed']
    max_episode_len = args['max_episode_len']

    plan_req = args['plan_dist_iters'] if 'plan_dist_iters' in args else None

    ####################
    accu_plan_time = 0
    accu_exe_time = 0
    accu_mask_time = 0
    ####################

    for it in range(args['max_iters']):

        if (it > 0) and (backup_rate > 0) and (it % backup_rate == 0) and (data_saver is not None):
            data_saver.save(episode_stats, ep_id=it)

        cur_infos = []
        motion.reset()
        set_seed(seed + it + 1)  # reset seed
        if plan_req is not None:
            while True:
                task.reset(target=fixed_target)
                m = len(task.get_optimal_plan())
                if (m in plan_req) and plan_req[m] > 0:
                    break
            plan_req[m] -= 1
        else:
            task.reset(target=fixed_target)
        info = task.info

        episode_success.append(0)
        episode_good.append(0)
        task_target = task.get_current_target()
        cur_stats = dict(best_dist=info['dist'],
                         success=0, good=0, reward=0, target=task_target,
                         plan=[],
                         meters=task.info['meters'], optstep=task.info['optsteps'], length=max_episode_len, images=None)
        if hasattr(task.house, "_id"):
            cur_stats['world_id'] = task.house._id

        store_history = args['store_history']
        if store_history:
            cur_infos.append(proc_info(task.info))

        episode_step = 0

        # reset planner
        if graph is not None:
            graph.reset()

        while episode_step < max_episode_len:
            if flag_interrupt and motion.is_interrupt():
                graph_target = task.get_current_target()
            else:
                # TODO #####################
                tt = time.time()
                mask_feat = oracle_func.get(task) if oracle_func is not None else task.get_feature_mask()
                accu_mask_time += time.time() - tt
                tt = time.time()
                graph_target = graph.plan(mask_feat, task_target)
                accu_plan_time += time.time() - tt
                ################################
            graph_target_id = common.target_instruction_dict[graph_target]
            allowed_steps = min(max_episode_len - episode_step, max_motion_steps)

            ###############
            # TODO
            tt = time.time()
            motion_data = motion.run(graph_target, allowed_steps)
            accu_exe_time += time.time() -tt

            cur_stats['plan'].append((graph_target, len(motion_data), (motion_data[-1][0][graph_target_id] > 0)))

            # store stats
            for dat in motion_data:
                info = dat[4]
                if store_history:
                    cur_infos.append(proc_info(info))
                cur_dist = info['dist']
                if cur_dist == 0:
                    cur_stats['good'] += 1
                    episode_good[-1] = 1
                if cur_dist < cur_stats['best_dist']:
                    cur_stats['best_dist'] = cur_dist

            # update graph
            ## TODO ############
            tt = time.time()
            graph.observe(motion_data, graph_target)
            accu_plan_time += time.time() - tt

            episode_step += len(motion_data)

            # check done
            if motion_data[-1][3]:
                if motion_data[-1][2] > 5: # magic number
                    episode_success[-1] = 1
                    cur_stats['success'] = 1
                break

        cur_stats['length'] = episode_step   # store length

        if store_history:
            cur_stats['infos'] = cur_infos
        episode_stats.append(cur_stats)

        dur = time.time() - elap
        logger.print('Episode#%d, Elapsed = %.3f min' % (it+1, dur/60))
        #TODO #################
        logger.print(' >>> Mask Time = %.4f min' % (accu_mask_time / 60))
        logger.print(' >>> Plan Time = %.4f min' % (accu_plan_time / 60))
        logger.print(' >>> Motion Time = %.4f min' % (accu_exe_time / 60))
        if args['multi_target']:
            logger.print('  ---> Target Room = {}'.format(cur_stats['target']))
        logger.print('  ---> Total Samples = {}'.format(t))
        logger.print('  ---> Success = %d  (rate = %.3f)'
                     % (cur_stats['success'], np.mean(episode_success)))
        logger.print('  ---> Times of Reaching Target Room = %d  (rate = %.3f)'
                     % (cur_stats['good'], np.mean(episode_good)))
        logger.print('  ---> Best Distance = %d' % cur_stats['best_dist'])
        logger.print('  ---> Birth-place Meters = %.4f (optstep = %d)' % (cur_stats['meters'], cur_stats['optstep']))
        logger.print('  ---> Planner Results = {}'.format(cur_stats['plan']))

    logger.print('######## Final Stats ###########')
    logger.print('Success Rate = %.3f' % np.mean(episode_success))
    logger.print('> Avg Ep-Length per Success = %.3f' % np.mean([s['length'] for s in episode_stats if s['success'] > 0]))
    logger.print('> Avg Birth-Meters per Success = %.3f' % np.mean([s['meters'] for s in episode_stats if s['success'] > 0]))
    logger.print('Reaching Target Rate = %.3f' % np.mean(episode_good))
    logger.print('> Avg Ep-Length per Target Reach = %.3f' % np.mean([s['length'] for s in episode_stats if s['good'] > 0]))
    logger.print('> Avg Birth-Meters per Target Reach = %.3f' % np.mean([s['meters'] for s in episode_stats if s['good'] > 0]))
    if args['multi_target']:
        all_targets = list(set([s['target'] for s in episode_stats]))
        for tar in all_targets:
            n = sum([1.0 for s in episode_stats if s['target'] == tar])
            succ = [float(s['success'] > 0) for s in episode_stats if s['target'] == tar]
            good = [float(s['good'] > 0) for s in episode_stats if s['target'] == tar]
            length = [s['length'] for s in episode_stats if s['target'] == tar]
            meters = [s['meters'] for s in episode_stats if s['target'] == tar]
            good_len = np.mean([l for l, g in zip(length, good) if g > 0.5])
            succ_len = np.mean([l for l, s in zip(length, succ) if s > 0.5])
            good_mts = np.mean([l for l, g in zip(meters, good) if g > 0.5])
            succ_mts = np.mean([l for l, s in zip(meters, succ) if s > 0.5])
            logger.print('>>>>> Multi-Target <%s>: Rate = %.3f (n=%d), Good = %.3f (AvgLen=%.3f; Mts=%.3f), Succ = %.3f (AvgLen=%.3f; Mts=%.3f)'
                % (tar, n / len(episode_stats), n, np.mean(good), good_len, good_mts, np.mean(succ), succ_len, succ_mts))

    return episode_stats


def parse_args():
    parser = argparse.ArgumentParser("Evaluation Locomotion for 3D House Navigation")
    # Select Task
    parser.add_argument("--task-name", choices=['roomnav', 'objnav'], default='roomnav')
    parser.add_argument("--false-rate", type=float, default=0, help='The Rate of Impossible Targets')
    # Environment
    parser.add_argument("--env-set", choices=['small', 'train', 'test', 'color'], default='small')
    parser.add_argument("--house", type=int, default=0, help="house ID")
    parser.add_argument("--render-gpu", type=int, help="gpu id for rendering the environment")
    parser.add_argument("--seed", type=int, default=0, help="random seed")
    parser.add_argument("--hardness", type=float, help="real number from 0 to 1, indicating the hardness of the environment")
    parser.add_argument("--max-birthplace-steps", type=int, help="int, the maximum steps required from birthplace to target")
    parser.add_argument("--min-birthplace-grids", type=int, default=0,
                        help="int, the minimum grid distance of the birthplace towards target. Default 0, namely possible to born with gird_dist=0.")
    parser.add_argument("--segmentation-input", choices=['none', 'index', 'color', 'joint'], default='none',
                        help="whether to use segmentation mask as input; default=none; <joint>: use both pixel input and color segment input")
    parser.add_argument("--resolution", choices=['normal', 'low', 'tiny', 'high', 'square', 'square_low'], default='normal',
                        help="resolution of visual input, default normal=[120 * 90]")
    parser.add_argument("--depth-input", dest='depth_input', action='store_true',
                        help="whether to include depth information as part of the input signal")
    parser.set_defaults(depth_input=False)
    parser.add_argument("--target-mask-input", dest='target_mask_input', action='store_true',
                        help="whether to include target mask 0/1 signal as part of the input signal")
    parser.set_defaults(target_mask_input=False)
    parser.add_argument("--success-measure", choices=['stop', 'stay', 'see'], default='see',
                        help="criteria for a successful episode")
    parser.add_argument("--terminate-measure", choices=['mask', 'stay', 'see'], default='mask',
                        help="criteria for terminating a motion execution")
    parser.add_argument("--interruptive-motion", dest='interruptive_motion', action='store_true',
                        help="[only affect for Obj-Nav with RNN/Mix Motion]")
    parser.set_defaults(interruptive_motion=False)
    parser.add_argument("--multi-target", dest='multi_target', action='store_true',
                        help="when this flag is set, a new target room will be selected per episode")
    parser.set_defaults(multi_target=False)
    parser.add_argument("--include-object-target", dest='object_target', action='store_true',
                        help="when this flag is set, target can be also a target. Only effective when --multi-target")
    parser.set_defaults(object_target=False)
    parser.add_argument("--no-outdoor-target", dest='outdoor_target', action='store_false',
                        help="when this flag is set, we will exclude <outdoor> target")
    parser.set_defaults(outdoor_target=True)
    parser.add_argument("--only-eval-room-target", dest='only_eval_room', action='store_true',
                        help="when this flag is set, only evaluate room targets. only effective when --include-object-target")
    parser.set_defaults(only_eval_room=False)
    parser.add_argument("--only-eval-object-target", dest='only_eval_object', action='store_true',
                        help="when this flag is set, only evaluate object targets. only effective when --include-object-target")
    parser.set_defaults(only_eval_object=False)
    parser.add_argument("--fixed-target", choices=common.ALLOWED_TARGET_ROOM_TYPES + common.ALLOWED_OBJECT_TARGET_TYPES + ['any-room', 'any-object'],
                        help="once set, all the episode will be fixed to a specific target.")
    # Core parameters
    parser.add_argument("--motion", choices=['rnn', 'fake', 'random', 'mixture'], default="fake", help="type of the locomotion")
    parser.add_argument("--random-motion-skill", type=int, default=6, help="skill rate for random motion, only effective when --motion random")
    parser.add_argument("--mixture-motion-dict", type=str, help="dict for mixture-motion, only effective when --motion mixture")
    parser.add_argument("--max-episode-len", type=int, default=2000, help="maximum episode length")
    parser.add_argument("--max-iters", type=int, default=1000, help="maximum number of eval episodes")
    parser.add_argument("--store-history", action='store_true', default=False, help="whether to store all the episode frames")
    parser.add_argument("--batch-norm", action='store_true', dest='use_batch_norm',
                        help="Whether to use batch normalization in the policy network. default=False.")
    parser.set_defaults(use_batch_norm=False)
    parser.add_argument("--use-target-gating", dest='target_gating', action='store_true',
                        help="[only affect when --multi-target] whether to use target instruction gating structure in the model")
    parser.set_defaults(target_gating=False)
    ######################
    # Regarding Plan-Dist
    ######################
    parser.add_argument("--plan-dist-iters", type=str,
                        help="Required iterations for each plan-distance birthplaces. In the format of Dist1:Number1,Dist2:Number2,...")
    # RNN Parameters
    parser.add_argument("--rnn-units", type=int,
                        help="[RNN-Only] number of units in an RNN cell")
    parser.add_argument("--rnn-layers", type=int,
                        help="[RNN-Only] number of layers in RNN")
    parser.add_argument("--rnn-cell", choices=['lstm', 'gru'],
                        help="[RNN-Only] RNN cell type")
    # Planner Parameters
    parser.add_argument("--planner", choices=['rnn', 'graph', 'random', 'oracle', 'void'], default='graph', help='type of the planner')
    parser.add_argument("--planner-filename", type=str, help='parameters for the planners')
    parser.add_argument("--planner-units", type=int, help='hidden units for planner, only effective when --planner rnn')
    parser.add_argument("--n-exp-steps", type=int, default=40, help='maximum number of steps for exploring a sub-policy')
    parser.add_argument("--planner-obs-noise", type=float, help="setting the parameters of observation noise")
    ##########################################    
    # Semantic Classifiers
    parser.add_argument('--semantic-dir', type=str, help='[SEMANTIC] root folder containing all semantic classifiers; or the path to the dictionary file')
    parser.add_argument('--semantic-threshold', type=float, default=0.85, help='[SEMANTIC] threshold for semantic labels. None: probability')
    parser.add_argument('--semantic-filter-steps', type=int, help="[SEMANTIC] filter steps (default, None)")
    parser.add_argument("--semantic-gpu", type=int, help="[SEMANTIC] gpu id for running semantic classifier")
    parser.add_argument("--semantic-batch-size", type=int, help="[SEMANTIC] group --batch-size of frames for fast semantic computation")
    parser.add_argument("--force-semantic-done", dest="force_oracle_done", action='store_true',
                        help="When flag set, agent will terminate its episode based on the semantic classifier.")
    parser.set_defaults(force_oracle_done=False)
    ##########################################
    # Checkpointing
    parser.add_argument("--backup-rate", type=int, default=0, help="when set > 0, store all the evaluation results every --backup-rate steps.")
    parser.add_argument("--log-dir", type=str, default="./log/eval", help="directory in which logs eval stats")
    parser.add_argument("--warmstart", type=str, help="file to load the policy model")
    parser.add_argument("--warmstart-dict", type=str, help="arg dict the policy model, only effective when --motion rnn")
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    assert (args.warmstart is None) or (os.path.exists(args.warmstart)), 'Model File Not Exists!'

    common.set_house_IDs(args.env_set, ensure_kitchen=(not args.multi_target))
    print('>> Environment Set = <%s>, Total %d Houses!' % (args.env_set, len(common.all_houseIDs)))

    if args.object_target:
        common.ensure_object_targets()

    if not os.path.exists(args.log_dir):
        print('Directory <{}> does not exist! Creating directory ...'.format(args.log_dir))
        os.makedirs(args.log_dir)

    if args.motion == 'rnn':
        assert args.warmstart is not None
    if args.motion == 'mixture':
        assert args.mixture_motion_dict is not None

    if args.fixed_target is None:
        if args.only_eval_room:
            args.fixed_target = 'any-room'
        elif args.only_eval_object:
            args.fixed_target = 'any-object'

    if args.seed is None:
        args.seed = 0

    if args.interruptive_motion:
        assert args.object_target
        assert args.motion in ['rnn', 'mixture']
        args.terminate_measure = "interrupt"
        print('--> Using Interruptive Terminate Measure!')

    if args.plan_dist_iters is not None:
        print('>> Parsing Plan Dist Iters ...')
        try:
            all_dist = args.plan_dist_iters.split(',')
            assert len(all_dist) > 0
            req = dict()
            total_req = 0
            for dat in all_dist:
                vals = dat.split(':')
                a, b = int(vals[0]), int(vals[1])
                assert (a > 0) and (b > 0) and (a not in req)
                req[a] = b
                total_req += b
            args.plan_dist_iters = req
            args.max_iters = total_req
            print(' ---> Parsing Done! Set Max-Iters to <{}>'.format(total_req))
            print('    >>> Details = {}'.format(req))
        except Exception as e:
            print('[ERROR] PlanDistIters Parsing Error for input <{}>!'.format(args.plan_dist_iters))
            raise e

    dict_args = args.__dict__


    class DataSaver:
        def __init__(self, _args):
            self.args = _args

        def save(self, data, ep_id=None):
            if self.args.store_history:
                _filename = self.args.log_dir
                if _filename[-1] != '/':
                    _filename += '/'
                _filename += self.args.motion

                if ep_id is not None:
                    _filename += '_ep{}_'.format(ep_id)
                else:
                    _filename += '_full_'
                _filename += 'eval_history.pkl'
                print('Saving stats to <{}> ...'.format(_filename))
                with open(_filename, 'wb') as _f:
                    pickle.dump([data, self.args], _f)
                print('  >> Done!')


    episode_stats = evaluate(dict_args, DataSaver(args))

    if args.store_history:
        filename = args.log_dir
        if filename[-1] != '/':
            filename += '/'
        filename += args.motion+'_full_eval_history.pkl'
        print('Saving all stats to <{}> ...'.format(filename))
        with open(filename, 'wb') as f:
            pickle.dump([episode_stats, args], f)
        print('  >> Done!')
