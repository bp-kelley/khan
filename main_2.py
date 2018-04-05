import os
import numpy as np
import time
import tensorflow as tf

from khan.training.trainer import Trainer, flatten_results
from khan.training.trainer_multi_gpu import TrainerMultiGPU, flatten_results

from data_utils import HARTREE_TO_KCAL_PER_MOL
from data_loaders import DataLoader
from concurrent.futures import ThreadPoolExecutor

import argparse

def main():

    parser = argparse.ArgumentParser(description="Run ANI1 neural net training.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--fitted', default=False, action='store_true', help="Whether or use fitted or self-ixn")
    parser.add_argument('--add_ffdata', default=False, action='store_true', help="Whether or not to add the forcefield data")
    parser.add_argument('--gpus', default=1, help="Number of gpus we use")

    parser.add_argument('--work-dir', default='~/work', help="location where work data is dumped")
    parser.add_argument('--train-dir', default='~/ANI-1_release', help="location where work data is dumped")

    args = parser.parse_args()

    print("Arguments", args)

    ANI_TRAIN_DIR = args.train_dir
    ANI_WORK_DIR = args.work_dir

    CALIBRATION_FILE_TRAIN = os.path.join(ANI_TRAIN_DIR, "results_QM_M06-2X.txt")
    CALIBRATION_FILE_TEST = os.path.join(ANI_TRAIN_DIR, "gdb_11_cal.txt")
    ROTAMER_TRAIN_DIR = os.path.join(ANI_TRAIN_DIR, "rotamers/train")
    ROTAMER_TEST_DIR = os.path.join(ANI_TRAIN_DIR, "rotamers/test")
    CHARGED_ROTAMER_TEST_DIR = os.path.join(ANI_TRAIN_DIR, "charged_rotamers_2")
    CCSDT_ROTAMER_TEST_DIR = os.path.join(ANI_TRAIN_DIR, "ccsdt_dataset")

    save_dir = os.path.join(ANI_WORK_DIR, "save")

    use_fitted = args.fitted
    add_ffdata = args.add_ffdata

    data_loader = DataLoader(use_fitted)

    if add_ffdata:
        ff_train_dir = ROTAMER_TRAIN_DIR
    else:
        ff_train_dir = None

    rd_train, rd_test = data_loader.load_gdb8(ANI_TRAIN_DIR, CALIBRATION_FILE_TRAIN, ff_train_dir)

    batch_size = 1024
    
    rd_gdb11 = data_loader.load_gdb11(ANI_TRAIN_DIR, CALIBRATION_FILE_TEST)
    rd_ffneutral_mo62x, ffneutral_groups_mo62x = data_loader.load_ff(ROTAMER_TEST_DIR)
    rd_ffneutral_ccsdt, ffneutral_groups_ccsdt = data_loader.load_ff(CCSDT_ROTAMER_TEST_DIR)
    rd_ffcharged_mo62x, ffcharged_groups_mo62x = data_loader.load_ff(CHARGED_ROTAMER_TEST_DIR)

    eval_names    = ["Neutral Rotamers", "Neutral Rotamers CCSDT", "Charged Rotamers"]
    eval_groups   = [ffneutral_groups_mo62x, ffneutral_groups_ccsdt, ffcharged_groups_mo62x]
    eval_datasets = [rd_ffneutral_mo62x, rd_ffneutral_ccsdt, rd_ffcharged_mo62x]

    config = tf.ConfigProto(allow_soft_placement=True)

    with tf.Session(config=config) as sess:

        # This training code implements cross-validation based training, whereby we determine convergence on a given
        # epoch depending on the cross-validation error for a given validation set. When a better cross-validation
        # score is detected, we save the model's parameters as the putative best found parameters. If after more than
        # max_local_epoch_count number of epochs have been run and no progress has been made, we decrease the learning
        # rate and restore the best found parameters.

        trainer = TrainerMultiGPU(sess, n_gpus=int(args.gpus))

        if os.path.exists(save_dir):
            print("Restoring existing model from", save_dir)
            trainer.load(save_dir)
        else:
            trainer.initialize() # initialize to random variables

        print("Evaluating Rotamer Errors:")

        for name, ff_data, ff_groups in zip(eval_names, eval_datasets, eval_groups):
            print(name, "abs/rel rmses: {0:.6f} kcal/mol | ".format(trainer.eval_abs_rmse(ff_data)) + "{0:.6f} kcal/mol".format(trainer.eval_eh_rmse(ff_data, ff_groups)))

        max_local_epoch_count = 100

        train_ops = [
            trainer.global_epoch_count,
            trainer.learning_rate,
            trainer.local_epoch_count,
            trainer.unordered_l2s,
            trainer.train_op
        ]

        best_test_score = trainer.eval_abs_rmse(rd_test)

        print("------------Starting Training--------------")

        start_time = time.time()

        while sess.run(trainer.learning_rate) > 5e-10: # this is to deal with a numerical error, we technically train to 1e-9

            while sess.run(trainer.local_epoch_count) < max_local_epoch_count:

                sess.run(trainer.max_norm_ops) # should this run after every batch instead?

                start_time = time.time()
                train_results = trainer.feed_dataset(
                    rd_train,
                    shuffle=True,
                    target_ops=train_ops,
                    batch_size=batch_size)

                global_epoch = train_results[0][0]
                time_per_epoch = time.time() - start_time
                train_abs_rmse = np.sqrt(np.mean(flatten_results(train_results, pos=3))) * HARTREE_TO_KCAL_PER_MOL
                learning_rate = train_results[0][1]
                local_epoch_count = train_results[0][2]

                test_abs_rmse = trainer.eval_abs_rmse(rd_test)
                print(time.strftime("%Y-%m-%d %H:%M:%S"), 'tpe:', "{0:.2f}s,".format(time_per_epoch), 'g-epoch', global_epoch, 'l-epoch', local_epoch_count, 'lr', "{0:.0e}".format(learning_rate), \
                    'train/test abs rmse:', "{0:.2f} kcal/mol,".format(train_abs_rmse), "{0:.2f} kcal/mol".format(test_abs_rmse), end='')

                if test_abs_rmse < best_test_score:
                    trainer.save_best_params()
                    gdb11_abs_rmse = trainer.eval_abs_rmse(rd_gdb11)
                    print(' | gdb11 abs rmse', "{0:.2f} kcal/mol | ".format(gdb11_abs_rmse), end='')
                    for name, ff_data, ff_groups in zip(eval_names, eval_datasets, eval_groups):
                        print(name, "abs/rel rmses", "{0:.2f} kcal/mol,".format(trainer.eval_abs_rmse(ff_data)), \
                            "{0:.2f} kcal/mol | ".format(trainer.eval_eh_rmse(ff_data, ff_groups)), end='')

                    best_test_score = test_abs_rmse
                    sess.run([trainer.incr_global_epoch_count, trainer.reset_local_epoch_count])
                else:
                    sess.run([trainer.incr_global_epoch_count, trainer.incr_local_epoch_count])

                trainer.save(save_dir)

                print('', end='\n')

            print("==========Decreasing learning rate==========")
            sess.run(trainer.decr_learning_rate)
            sess.run(trainer.reset_local_epoch_count)
            trainer.load_best_params()

    return



if __name__ == "__main__":

    from tensorflow.python.client import device_lib

    def get_available_gpus():
        local_device_protos = device_lib.list_local_devices()
        return [x.name for x in local_device_protos if x.device_type == 'GPU']

    avail_gpus = get_available_gpus()

    print("Available GPUs:", avail_gpus)

    if len(avail_gpus) == 0:
        raise Exception("No GPUs available to this tensorflow process.")

    main()




