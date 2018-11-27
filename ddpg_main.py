import os
import random
import datetime
import tensorflow as tf
import yaml
import time
import numpy as np

from curriculum_manager import CurriculumManager
from hindsight_policy import HindsightPolicy
from network import Network
from replay_buffer import ReplayBuffer
from rollout_manager import RolloutManager
from summaries_collector import SummariesCollector
from trajectory_eval import TrajectoryEval
from pre_trained_reward import PreTrainedReward
from workspace_generation_utils import *


def run_for_config(config, print_messages):
    # set the name of the model
    model_name = config['general']['name']
    now = datetime.datetime.fromtimestamp(time.time()).strftime('%Y_%m_%d_%H_%M_%S')
    model_name = now + '_' + model_name if model_name is not None else now

    # openrave_interface = OpenraveRLInterface(config, None)
    random_seed = config['general']['random_seed']
    np.random.seed(random_seed)
    random.seed(random_seed)
    tf.set_random_seed(random_seed)

    # where we save all the outputs
    working_dir = os.getcwd()
    saver_dir = os.path.join(working_dir, 'models', model_name)
    if not os.path.exists(saver_dir):
        os.makedirs(saver_dir)
    config_copy_path = os.path.join(working_dir, 'models', model_name, 'config.yml')
    summaries_dir = os.path.join(working_dir, 'tensorboard', model_name)
    completed_trajectories_dir = os.path.join(working_dir, 'trajectories', model_name)

    # load pretrained model if required
    pre_trained_reward = None
    reward_model_name = config['model']['reward_model_name']
    if reward_model_name is not None:
        pre_trained_reward = PreTrainedReward(reward_model_name, config)

    # generate graph:
    network = Network(config, is_rollout_agent=False, pre_trained_reward=pre_trained_reward)

    def unpack_state_batch(state_batch):
        joints = [state[0] for state in state_batch]
        poses = {p.tuple: [state[1][p.tuple] for state in state_batch] for p in network.potential_points}
        jacobians = None
        return joints, poses, jacobians

    def score_for_hindsight(augmented_buffer):
        # unzip
        goal_pose_list, goal_joints_list, workspace_image_list, current_state_list, action_used_list, _, is_goal_list,\
        __ = zip(*augmented_buffer)
        # make one hot status vector:
        is_goal_one_hot_list = np.zeros((len(is_goal_list), 3), dtype=np.float32)
        for i in range(len(is_goal_list)):
            if is_goal_list[i]:
                is_goal_one_hot_list[i, 2] = 1.0  # mark as goal transition
            else:
                is_goal_one_hot_list[i, 0] = 1.0  # mark as free transition
        # unpack current and next state
        current_joints, _, __ = unpack_state_batch(current_state_list)

        fake_rewards, _ = pre_trained_reward.make_prediction(
            sess, current_joints, goal_joints_list, action_used_list, goal_pose_list,
            all_transition_labels=is_goal_one_hot_list
        )
        return list(fake_rewards)

    # initialize replay memory
    replay_buffer = ReplayBuffer(config)
    hindsight_policy = HindsightPolicy(config, replay_buffer, score_for_hindsight)

    # save model
    saver = tf.train.Saver(max_to_keep=4, save_relative_paths=saver_dir)
    yaml.dump(config, open(config_copy_path, 'w'))
    summaries_collector = SummariesCollector(summaries_dir, model_name)
    curriculum_manager = CurriculumManager(config, print_messages)
    rollout_manager = RolloutManager(config)

    test_results = []

    def update_model(sess, global_step):
        batch_size = config['model']['batch_size']
        gamma = config['model']['gamma']
        replay_buffer_batch = replay_buffer.sample_batch(batch_size)

        goal_pose, goal_joints, workspace_image, current_state, action, reward, terminated, next_state = \
            replay_buffer_batch

        current_joints, current_poses, current_jacobians = unpack_state_batch(current_state)
        next_joints, next_poses, next_jacobians = unpack_state_batch(next_state)

        # get the predicted q value of the next state (action is taken from the target policy)
        next_state_action_target_q = network.predict_policy_q(
            next_joints, workspace_image, goal_pose, goal_joints, sess, use_online_network=False
        )

        # compute critic label
        q_label = np.expand_dims(np.array(reward) + np.multiply(
            np.multiply(1 - np.array(terminated), gamma),
            np.squeeze(next_state_action_target_q)
        ), 1)
        max_label = np.max(q_label)
        min_label = np.min(q_label)
        limit = 1.0 / (1.0 - gamma)
        if max_label > limit:
            print 'out of range max label: {} limit: {}'.format(max_label, limit)
        if min_label < -limit:
            print 'out of range min label: {} limit: {}'.format(min_label, limit)

        # # step to use for debug:
        # network.debug_all(current_joints, workspace_image, goal_pose, goal_joints, action, q_label, sess)

        # train critic given the targets
        critic_optimization_summaries, _ = network.train_critic(
            current_joints, workspace_image, goal_pose, goal_joints, action, q_label, sess
        )

        # train actor
        actor_optimization_summaries, _ = network.train_actor(
            current_joints, workspace_image, goal_pose, goal_joints, sess
        )

        # update target networks
        network.update_target_networks(sess)

        result = [critic_optimization_summaries, actor_optimization_summaries, ]
        return result

    def alter_episode(status, states, actions, rewards, goal_pose, goal_joints, workspace_image):
        alter_episode_mode = config['model']['alter_episode']
        if alter_episode_mode == 0:
            return status, states, actions, rewards, goal_pose, goal_joints, workspace_image
        assert pre_trained_reward is not None
        # first unpack states:
        joints, poses, jacobians = unpack_state_batch(states)
        current_joints = joints[:len(joints)-1]
        #  make a prediction
        if alter_episode_mode == 2:
            # change the rewards but keep episode the same, use he status in the reward prediction
            one_hot_status = np.zeros((len(rewards), 3), dtype=np.float32)
            one_hot_status[:-1, 0] = 1.0
            one_hot_status[-1, 2] = 1.0
            fake_rewards = pre_trained_reward.make_prediction(
                sess, current_joints, [goal_joints] * len(actions), actions, [goal_pose] * len(actions),
                all_transition_labels=one_hot_status
            )[0]
            return status, states, actions, [r[0] for r in fake_rewards], goal_pose, goal_joints, workspace_image
        if alter_episode_mode == 1:
            # get the maximal status for each transition (the indices are from 0-2 while the status are 1-3)
            fake_rewards, fake_status_prob = pre_trained_reward.make_prediction(
                sess, current_joints, [goal_joints]*len(actions), actions, [goal_pose]*len(actions))
            fake_status = np.argmax(np.array(fake_status_prob), axis=1)
            fake_status += 1
            # iterate over approximated episode and see if truncation is needed
            truncation_index = 0
            for truncation_index in range(len(fake_status)):
                if fake_status[truncation_index] != 1:
                    break
            # return the status of the last transition, truncated list of states and actions, the fake rewards (also
            # truncated) and the goal parameters as-is.
            altered_status = fake_status[truncation_index]
            altered_states = states[:truncation_index+2]
            altered_actions = actions[:truncation_index+1]
            altered_rewards = [r[0] for r in fake_rewards[:truncation_index+1]]
            return altered_status, altered_states, altered_actions, altered_rewards, goal_pose, goal_joints, \
                   workspace_image

    def print_state(prefix, episodes, successful_episodes, collision_episodes, max_len_episodes):
        if not print_messages:
            return
        print '{}: {}: finished: {}, successful: {} ({}), collision: {} ({}), max length: {} ({})'.format(
            datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S'), prefix, episodes,
            successful_episodes, float(successful_episodes) / episodes, collision_episodes,
            float(collision_episodes) / episodes, max_len_episodes, float(max_len_episodes) / episodes
        )

    def process_example_trajectory(
            example_trajectory, example_trajectory_poses, goal_pose, goal_joints, workspace_image):
        example_trajectory = [j[1:] for j in example_trajectory]
        # goal reached always
        status = 3
        # get the states (joints, poses, jacobians), for now, ignore the jacobians.
        states = [(example_trajectory[i], example_trajectory_poses[i], None) for i in range(len(example_trajectory))]
        # compute the actions by normalized difference between steps
        actions = [np.array(example_trajectory[i+1]) - np.array(example_trajectory[i])
                   for i in range(len(example_trajectory)-1)]
        actions = [a / max(np.linalg.norm(a), 0.00001) for a in actions]
        # compute the rewards
        one_hot_status = np.zeros((len(actions), 3), dtype=np.float32)
        one_hot_status[:-1, 0] = 1.0
        one_hot_status[-1, 2] = 1.0
        fake_rewards = pre_trained_reward.make_prediction(
            sess, example_trajectory[:-1], [goal_joints] * len(actions), actions, [goal_pose] * len(actions),
            all_transition_labels=one_hot_status
        )[0]
        return status, states, actions, fake_rewards, goal_pose, goal_joints, workspace_image

    with tf.Session(
            config=tf.ConfigProto(
                gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=config['general']['gpu_usage'])
            )
    ) as sess:
        sess.run(tf.global_variables_initializer())
        if pre_trained_reward is not None:
            pre_trained_reward.load_weights(sess)
        network.update_target_networks(sess)

        trajectory_eval = TrajectoryEval(config, rollout_manager, completed_trajectories_dir)

        global_step = 0
        total_episodes = episodes = successful_episodes = collision_episodes = max_len_episodes = 0
        test_episodes = test_successful_episodes = 0
        for update_index in range(config['general']['updates_cycle_count']):
            allowed_size, has_changed = curriculum_manager.get_next_parameters(test_episodes, test_successful_episodes)
            # allowed_size, has_changed = curriculum_manager.get_next_parameters(episodes, successful_episodes)
            if has_changed:
                test_episodes = test_successful_episodes = 0
                # episodes = successful_episodes = collision_episodes = max_len_episodes = 0

            # collect data
            a = datetime.datetime.now()
            rollout_manager.set_policy_weights(network.get_actor_online_weights(sess))
            episodes_per_update = config['general']['episodes_per_update']
            episode_results = rollout_manager.generate_episodes(episodes_per_update, True)
            added_failed_trajectories = 0
            total_find_trajectory_time = None
            total_rollout_time = None
            for episode_result in episode_results:
                # single episode execution:
                episode_agent_trajectory, episode_times, episode_example_trajectory = episode_result
                # update the times
                find_trajectory_time, rollout_time = episode_times
                if total_find_trajectory_time is None:
                    total_find_trajectory_time = find_trajectory_time
                else:
                    total_find_trajectory_time += find_trajectory_time
                if total_rollout_time is None:
                    total_rollout_time = rollout_time
                else:
                    total_rollout_time += rollout_time
                # post process the episode
                status, states, actions, rewards, goal_pose, goal_joints, workspace_image, = alter_episode(*episode_agent_trajectory)
                # at the end of episode, append to buffer
                hindsight_policy.append_to_replay_buffer(
                    status, states, actions, rewards, goal_pose, goal_joints, workspace_image
                )
                # if the episode failed, and we want to use the motion planners trajectories, add to buffer:
                if status != 3 and added_failed_trajectories < config['model']['failed_motion_planner_trajectories']:
                    assert pre_trained_reward is not None
                    motion_planner_trajectory, motion_planner_trajectory_poses = episode_example_trajectory
                    added_failed_trajectories += 1
                    hindsight_policy.append_to_replay_buffer(*process_example_trajectory(
                        motion_planner_trajectory, motion_planner_trajectory_poses, goal_pose, goal_joints, workspace_image
                    ))
                total_episodes += 1
                episodes += 1
                if status == 1:
                    max_len_episodes += 1
                elif status == 2:
                    collision_episodes += 1
                elif status == 3:
                    successful_episodes += 1
            b = datetime.datetime.now()
            print 'data collection took: {}'.format(b-a)
            print 'find trajectory took: {}'.format(total_find_trajectory_time)
            print 'rollout time took: {}'.format(total_rollout_time)
            print_state('train', episodes, successful_episodes, collision_episodes, max_len_episodes)

            # do updates
            if replay_buffer.size() > config['model']['batch_size']:
                a = datetime.datetime.now()
                for _ in range(config['general']['model_updates_per_cycle']):
                    summaries = update_model(sess, global_step)
                    if global_step % config['general']['write_train_summaries'] == 0:
                        summaries_collector.write_train_episode_summaries(
                            sess, global_step, episodes, successful_episodes, collision_episodes, max_len_episodes
                        )
                        summaries_collector.write_train_optimization_summaries(summaries, global_step)
                    global_step += 1
                b = datetime.datetime.now()
                print 'update took: {}'.format(b - a)

            # test if needed
            if update_index % config['test']['test_every_cycles'] == 0:
                rollout_manager.set_policy_weights(network.get_actor_online_weights(sess))
                eval_result = trajectory_eval.eval(global_step, allowed_size)
                test_episodes = eval_result[0]
                test_successful_episodes = eval_result[1]
                test_collision_episodes = eval_result[2]
                test_max_len_episodes = eval_result[3]
                test_mean_reward = eval_result[4]
                if print_messages:
                    print('test path allowed length {}'.format(allowed_size))
                    print_state('test', test_episodes, test_successful_episodes, test_collision_episodes,
                                test_max_len_episodes)
                    print('test mean total reward {}'.format(test_mean_reward))
                summaries_collector.write_test_episode_summaries(
                    sess, global_step, test_episodes, test_successful_episodes, test_collision_episodes,
                    test_max_len_episodes
                )
                summaries_collector.write_test_curriculum_summaries(sess, global_step, allowed_size)
                test_results.append((global_step, episodes, test_successful_episodes, allowed_size))

            if update_index % config['general']['save_model_every_cycles'] == 0:
                saver.save(sess, os.path.join(saver_dir, 'all_graph'), global_step=global_step)
    rollout_manager.end()
    return test_results


if __name__ == '__main__':
    # disable tf warning
    # os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

    # read the config
    config_path = os.path.join(os.getcwd(), 'config/config.yml')
    with open(config_path, 'r') as yml_file:
        config = yaml.load(yml_file)
        print('------------ Config ------------')
        print(yaml.dump(config))

    run_for_config(config, print_messages=True)
