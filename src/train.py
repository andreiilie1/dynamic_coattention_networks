import tensorflow as tf
import highway_max_out
import encoder
import ciprian_data_prep_script
import tfrecord_converter
import argparse
import time
import datetime
import os
import utils
import random
from tensorflow.python.client import timeline
from tensorflow.python import debug as tf_debug


start_time = time.time()

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

import logging
logging.getLogger('tensorflow').setLevel(50)

prob = tf.placeholder_with_default(0.7, shape=())

parser = argparse.ArgumentParser()
parser.add_argument("--num_epochs", default=200, type=int, help="Number of epochs to train for")
parser.add_argument("--restore", action="store_true", default=False, help="Whether to restore weights from previous run")
#parser.add_argument("--num_units", default=200, type=int,
#	help="Number of recurrent units for the first lstm, which is deteriministic and is only used in both training and testing")
parser.add_argument("--test", "-t", default=False, action="store_true", help="Whether to run in test mode")
parser.add_argument("--batch_size", default=64, type=int, help="Size of each training batch")
parser.add_argument("--dataset", choices=["SQuAD"],default="SQuAD", type=str, help="Dataset to train and evaluate on")
parser.add_argument("--hidden_size", default=200, type=int, help="Size of the hidden state")
parser.add_argument("--keep_prob", default=prob, type=float, help="Keep probability for question and document encodings.")
parser.add_argument("--learning_rate", default=0.001, type=float, help="Learning rate.")
parser.add_argument("--short_test", "-s", default=False, action="store_true", help="Whether to run in short test mode")
parser.add_argument("--pool_size", default=16, type=int, help="Number of units to pool over in HMN sub-network")
parser.add_argument("--tfdbg", default=False, action="store_true", help="Whether to enter tf debugger")
#parser.add_argument("--validate", default=False, action="store_true", help="Whether to apply validation.")
#parser.add_argument("--early_stop", default=None, type=int, 
#	help="Number of epochs without improvement before applying early-stopping. Defaults to num_epochs, which amounts to no early-stopping.")


ARGS = parser.parse_args()
keep_probability = ARGS.keep_prob

if ARGS.test:
    print("Running in test mode")


with tf.name_scope("data_prep"):
    input_d_vecs, input_q_vecs, ground_truth_labels, documents_lengths, questions_lengths, _, _ = ciprian_data_prep_script.get_data("train")
    input_d_vecs_validation, input_q_vecs_validation, ground_truth_labels_validation, documents_lengths_validation, questions_lengths_validation, questions_ids_validation, all_answers_validation = ciprian_data_prep_script.get_data("test")
    print("In train.py: get_data finished.")

    start_l = list(map(lambda x: x[0], ground_truth_labels))
    end_l = list(map(lambda x: x[1], ground_truth_labels))

    start_l_validation = list(map(lambda x: x[0], ground_truth_labels_validation))
    end_l_validation = list(map(lambda x: x[1], ground_truth_labels_validation))

    d = tf.placeholder(tf.float64, [ARGS.batch_size, len(input_d_vecs[0]), len(input_d_vecs[0][0])])
    q = tf.placeholder(tf.float64, [ARGS.batch_size, len(input_q_vecs[0]), len(input_q_vecs[0][0])])
    starting_labels = tf.placeholder(tf.int64, [ARGS.batch_size])
    ending_labels = tf.placeholder(tf.int64, [ARGS.batch_size])
    doc_l = tf.placeholder(tf.int64, [ARGS.batch_size])
    que_l = tf.placeholder(tf.int64, [ARGS.batch_size])

with tf.name_scope("performance_metrics"):
    em_score_log = tf.placeholder(tf.float32, ())
    f1_score_log = tf.placeholder(tf.float32, ())
    tf.summary.scalar('em_score', em_score_log)
    tf.summary.scalar('f1_score', f1_score_log)

with tf.name_scope("parameters"):
    tf.summary.scalar('batch_size', tf.constant(ARGS.batch_size))
    tf.summary.scalar('hidden_size', tf.constant(ARGS.hidden_size))
    tf.summary.scalar('pool_size', tf.constant(ARGS.pool_size))
    tf.summary.scalar('keep_prob', ARGS.keep_prob)
    tf.summary.scalar('learning_rate', tf.constant(ARGS.learning_rate))


with tf.name_scope("encoder"):
    encoded = encoder.encoder(
        document=d,
        question=q,
        documents_lengths=doc_l,
        questions_lengths=que_l,
        hyperparameters=ARGS
    )

    # Create single nodes for labels, data version
    #start_labels = tf.one_hot(a[:,0], 600)
    #end_labels = tf.one_hot(a[:,1], 600)

    # Create single nodes for labels, feed_dict version
    start_labels = tf.one_hot(starting_labels, 600)
    end_labels = tf.one_hot(ending_labels, 600)

with tf.name_scope("decoder"):
    # Calculate padding mask
    zeros = tf.zeros([ARGS.batch_size, 600, 2*ARGS.hidden_size])
    zeros_set = tf.equal(encoded, zeros)
    padding_mask_bool = tf.reduce_all(zeros_set, 2)
    words_mask_bool = tf.logical_not(padding_mask_bool)
    padding_mask = tf.cast(padding_mask_bool, tf.float32)
    words_mask = tf.cast(words_mask_bool, tf.float32)

    # A tensor filled with the minimum float values
    min_float = tf.fill([ARGS.batch_size, 600], tf.cast(tf.float32.min, tf.float32))

    # Persistent loss mask that remembers whether convergence has already taken place
    loss_mask = tf.fill([ARGS.batch_size], True)
    s_prev = tf.fill([ARGS.batch_size], -1)
    e_prev = tf.fill([ARGS.batch_size], -1)

    # Static tensor to be re-used in main loop iterations
    batch_indices = tf.range(start=0, limit=ARGS.batch_size, dtype=tf.int32)

    # Create and initialize decoding LSTM
    decoding_lstm = tf.contrib.rnn.LSTMCell(num_units=ARGS.hidden_size)
    h = decoding_lstm.zero_state(ARGS.batch_size, dtype=tf.float32)

    # First guess for start- and end-points
    u_s = tf.zeros([ARGS.batch_size, 2 * ARGS.hidden_size])  # Dummy guess start point
    u_e = tf.zeros([ARGS.batch_size, 2 * ARGS.hidden_size])  # Dummy guess end point

    with tf.name_scope("decoding_loop"):
        for i in range(4):
            # LSTM input is concatenation of previous guesses
            usue = tf.concat([u_s, u_e], axis=1)
            lstm_output, h = decoding_lstm(inputs=usue, state=h)
            # Make an estimation of start- and end-points. Define the
            # set of graph nodes for the HMN twice in two different namespaces
            # so that two copies are created and they can be trained separately

            with tf.variable_scope('start_estimator'):
                alphas = highway_max_out.HMN(
                    current_words=encoded,
                    lstm_hidden_state=lstm_output,
                    prev_start_point_guess=u_s,
                    prev_end_point_guess=u_e,
                    pool_size=ARGS.pool_size,
                    h_size=ARGS.hidden_size,
                    name="HMN_start"
                )

            with tf.variable_scope('end_estimator'):
                betas = highway_max_out.HMN(
                    current_words=encoded,
                    lstm_hidden_state=lstm_output,
                    prev_start_point_guess=u_s,
                    prev_end_point_guess=u_e,
                    pool_size=ARGS.pool_size,
                    h_size=ARGS.hidden_size,
                    name="HMN_end"
                )

            alphas = tf.multiply(alphas, words_mask)
            alphas = tf.add(alphas, tf.multiply(min_float, padding_mask))
            s = tf.argmax(alphas, axis=1, output_type=tf.int32)
            s_encoding_indices = tf.transpose(tf.stack([batch_indices, s]))
            u_s = tf.gather_nd(encoded, s_encoding_indices)

            betas = tf.multiply(betas, words_mask)
            betas = tf.add(betas, tf.multiply(min_float, padding_mask))
            e = tf.argmax(betas, axis=1, output_type=tf.int32)
            e_encoding_indices = tf.transpose(tf.stack([batch_indices, e]))
            u_e = tf.gather_nd(encoded, e_encoding_indices)

            # Each guess contributes to loss,
            # even the very first
            s_loss = tf.nn.softmax_cross_entropy_with_logits_v2(
                labels=start_labels,
                logits=alphas
            )
            e_loss = tf.nn.softmax_cross_entropy_with_logits_v2(
                labels=end_labels,
                logits=betas
            )

            with tf.name_scope("iteration_" + str(i) + "_loss"):
                s_mask = tf.equal(s, s_prev)
                e_mask = tf.equal(e, e_prev)
                output_same = tf.logical_and(s_mask, e_mask)
                s_prev = s
                e_prev = e
                loss_mask = tf.logical_and(loss_mask, tf.logical_not(output_same))
                iteration_loss = s_loss + e_loss
                masked_iteration_loss = tf.multiply(iteration_loss, tf.cast(loss_mask, tf.float32))
                tf.summary.scalar('loss', tf.reduce_mean(iteration_loss))
                tf.summary.scalar('masked_it_loss_summary', tf.reduce_mean(masked_iteration_loss))

            #loss = iteration_loss if i == 0 else loss + iteration_loss
            with tf.control_dependencies([tf.assert_greater_equal(iteration_loss, masked_iteration_loss)]):
                loss = masked_iteration_loss if i == 0 else loss + masked_iteration_loss

            final_s = s if i == 0 else final_s + tf.multiply(tf.cast(loss_mask, tf.int32), s - final_s)
            final_e = e if i == 0 else final_e + tf.multiply(tf.cast(loss_mask, tf.int32), e - final_e)

mean_loss = tf.reduce_mean(loss)
tf.summary.scalar('total_loss', mean_loss)
optimizer = tf.train.AdamOptimizer(ARGS.learning_rate)
train_step = optimizer.minimize(mean_loss)

# For Tensorboard
merged = tf.summary.merge_all()
#summaryDirectory = "/home/shared/summaries/start_" + str(datetime.datetime.now())
summaryDirectory = "summaries/"
tf.gfile.MkDir(summaryDirectory)
summaryDirectory += str(datetime.datetime.now())
summaryDirectory = summaryDirectory.replace('.', '_').replace(':', '-').replace(' ', '_')
tf.gfile.MkDir(summaryDirectory)
writer = tf.summary.FileWriter(summaryDirectory)
writer.add_graph(tf.get_default_graph())

saver = tf.train.Saver(max_to_keep = 100)

time_now = datetime.datetime.now()
logDirectory = "logs/"
tf.gfile.MkDir(logDirectory)
file_name = "log" + str(time_now) + ".txt"
file_name = file_name.replace(':', '-').replace(' ', '_')
file = open(logDirectory + file_name, "w")

fileEM_name = "logEM" + str(time_now) + ".txt"
fileEM_name = fileEM_name.replace(':', '-').replace(' ', '_')
fileEM = open(logDirectory + fileEM_name, "w")
fileEM.write("Hyperparameters:" + str(ARGS))

soft_min = tf.nn.softmax(end_labels)

if ARGS.tfdbg:
    chosen_session = tf_debug.LocalCLIDebugWrapperSession(tf.Session())
else:
    chosen_session = tf.Session()
with chosen_session as sess:
    sess.run(tf.global_variables_initializer())
    train_start_time = time.time()
    print("Graph-build time: ", utils.time_format(train_start_time - start_time))

    dataset_length = len(input_d_vecs)
    num_batchs = dataset_length // ARGS.batch_size 

    dataset_length_validation = len(input_d_vecs_validation)
    num_batchs_validation = dataset_length_validation // ARGS.batch_size

    best_em_score = 0.0
    best_avg_f1 = 0.0
    global_batch_num = 0
    for epoch in range(ARGS.num_epochs):

        print("\nEpoch:", epoch)
        file.write("Epoch: " + str(epoch) + "\n")

        batch_size = ARGS.batch_size
        shuffling = list(zip(input_d_vecs, input_q_vecs, start_l, end_l, documents_lengths, questions_lengths))
        random.shuffle(shuffling) 
        input_d_vecs, input_q_vecs, start_l, end_l, documents_lengths, questions_lengths = zip(*shuffling)

        new_avg_f1 = 0
        new_em_score = 0
        epoch_loss = 0
        for dp_index in range(0, dataset_length, batch_size):
            if dp_index + batch_size > dataset_length:
                break
            #batch_time = time.time()
            feed_dict = {
                d: input_d_vecs[dp_index:dp_index + batch_size],
                q: input_q_vecs[dp_index: dp_index + batch_size],
                starting_labels: start_l[dp_index: dp_index + batch_size],
                ending_labels: end_l[dp_index: dp_index + batch_size],
                doc_l: documents_lengths[dp_index: dp_index + batch_size],
                que_l: questions_lengths[dp_index: dp_index + batch_size],
                f1_score_log: new_avg_f1,
                em_score_log: new_em_score
                }

            if dp_index//batch_size % 10 == 0: #or batch_size == dataset_length-batch_num:
                _, loss_val, summary_val, = sess.run([train_step, mean_loss, merged], feed_dict=feed_dict)
                writer.add_summary(summary_val, global_batch_num)
                print("\tBatch: {}\tloss: {}".format(dp_index // batch_size, loss_val))
                epoch_loss += loss_val
            else:
                sess.run([train_step], feed_dict=feed_dict)

            global_batch_num += 1

            #print(time.time()-batch_time)
            if ARGS.test:
                break

        epoch_loss = epoch_loss/(int(dataset_length/batch_size))
        total_count = 0.1
        exact_matches = 0.1
        running_f1 = 0.1

        for dp_index_validation in range(0, dataset_length_validation, ARGS.batch_size):
            if(dp_index_validation % 500 == 0):
                print("Validation Batch: ", dp_index_validation, "\n")
                file.write("Validation Batch: " + str(dp_index_validation) + "\n")
                file.flush()
            if dp_index_validation + ARGS.batch_size >= len(input_d_vecs_validation):
                break

            feed_dict_validation = {
            	prob: 1,
                d: input_d_vecs_validation[dp_index_validation:dp_index_validation + ARGS.batch_size],
                q: input_q_vecs_validation[dp_index_validation: dp_index_validation + ARGS.batch_size],
                starting_labels: start_l_validation[dp_index_validation: dp_index_validation + ARGS.batch_size],
                ending_labels: end_l_validation[dp_index_validation: dp_index_validation + ARGS.batch_size],
                doc_l: documents_lengths_validation[dp_index_validation: dp_index_validation + ARGS.batch_size],
                que_l: questions_lengths_validation[dp_index_validation: dp_index_validation + ARGS.batch_size]
                }

            loss_val_validation, start_predict_validation, end_predict_validation = sess.run([mean_loss, final_s, final_e], feed_dict = feed_dict_validation)
            start_correct_validation = start_l_validation[dp_index_validation: dp_index_validation + ARGS.batch_size]
            end_correct_validation = end_l_validation[dp_index_validation: dp_index_validation + ARGS.batch_size]

            for i in range(ARGS.batch_size):
                total_count += 1.0
                got_exact_match = False
                best_f1_dp = 0.0
                for ans in all_answers_validation[dp_index_validation + i]:
                    new_f1_dp = utils.compute_f1_from_indices(start_predict_validation[i], end_predict_validation[i], ans[0], ans[1])
                    if new_f1_dp > best_f1_dp:
                        best_f1_dp = new_f1_dp
                    if ans == [start_predict_validation[i], end_predict_validation[i]]:
                        got_exact_match = True
                if got_exact_match:
                    exact_matches += 1
                running_f1 += best_f1_dp

                if(dp_index_validation % 100 == 0):
                    file.write("Question with ID: " + str(questions_ids_validation[dp_index_validation + i]))
                    file.write("\n")
                    file.write("Correct (start, end): " + str(all_answers_validation[dp_index_validation + i]))
                    file.write("\n")
                    file.write("Predicted (start, end): " + str((start_predict_validation[i], end_predict_validation[i])))
                    file.write("\n")
                    file.write("___________________________\n")
                    file.flush()

            if ARGS.test:
                break
            if dp_index_validation == 100:
                break
        new_em_score = exact_matches / total_count
        new_avg_f1 = running_f1 / total_count
        if new_avg_f1 > best_avg_f1:
            best_avg_f1 = new_avg_f1

        print("New avg f1: %f Best avg f1: %f." % (new_avg_f1, best_avg_f1))
        if new_em_score > best_em_score:
            #save_path = saver.save(sess, "checkpoints/model.ckpt")
            save_path = saver.save(sess, "checkpoints/model{}.ckpt".format(epoch))
            print("EM score improved from %f to %f. Model saved in path: %s" % (best_em_score, new_em_score, save_path,))
            fileEM.write("Epoch number:" + str(epoch))
            fileEM.write("\n")
            fileEM.write("\nNew EM score:" + str(new_em_score) + " Best EM score: " + str(best_em_score) + ". Model saved in path: " + str(save_path))
            fileEM.write("\nNew avg F1:" + str(new_avg_f1) + " Best avg f1: " + str(best_avg_f1) + ".")
            #fileEM.write("\n")
            fileEM.write("\nEpoch loss value:" + str(epoch_loss))
            fileEM.write("\n\n")
            fileEM.flush()
            best_em_score = new_em_score 
        else:
            print("No improvement in EM score, not saving")
            fileEM.write("Epoch number:" + str(epoch))
            fileEM.write("\n")
            fileEM.write("\nNew EM score:" + str(new_em_score) + " Best EM score: " + str(best_em_score) + ". No improvement, model not saved.")
            fileEM.write("\nNew avg F1:" + str(new_avg_f1) + " Best avg f1: " + str(best_avg_f1) + ".")
            #fileEM.write("\n")
            fileEM.write("\nEpoch loss value:" + str(epoch_loss))
            fileEM.write("\n\n")
            fileEM.flush()
 
train_end_time = time.time()

print("Train time", utils.time_format(train_end_time - train_start_time))
"""
with tf.Session() as sess:
    sess.run(tf.global_variables_initializer())
    train_start_time = time.time()
    print("Graph-build time: ", utils.time_format(train_start_time - start_time))
    for i in range(ARGS.num_epochs):
        sess.run(train_init_op)
        profileFirstBatch = False
        while True:
            try:
                if profileFirstBatch:
                    profileFirstBatch = False
                    options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()

                    summary, _, loss_val = sess.run([merged, train_step, mean_loss],
                                                    options=options,
                                                    run_metadata=run_metadata)

                    fetched_timeline = timeline.Timeline(run_metadata.step_stats)
                    chrome_trace = fetched_timeline.generate_chrome_trace_format()
                    with open('profiling_timeline.json', 'w') as f:
                        f.write(chrome_trace)
                else:
                    summary, _, loss_val = sess.run([merged, train_step, mean_loss])
                    print(loss_val)
                    if ARGS.test:
                        break
            except tf.errors.OutOfRangeError:
                writer.add_summary(summary, i)
                if ARGS.validate:
                    print('\nComputing validiation loss:')
                    sess.run(val_init_op)
                    new_loss_val = 0
                    while True:
                        try:
                            new_loss_val += sess.run(mean_loss)
                        except tf.errors.OutOfRangeError:
                            print(new_loss_val)
                            if new_loss_val < best_loss_val or best_loss_val == -1:
                                best_loss_val = new_loss_val
                                save_path = saver.save(sess, "/home/shared/checkpoints/model.ckpt")
                                print("Model saved in path: %s" % save_path)
                            else:
                                print("No improvement in loss, not saving")
                                tolerance -= 1
                            break
                break           

        if tolerance == 0:
            print('Tolerance threshold reached, early-stopping')
            break

    train_end_time = time.time()

    print("Train time", utils.time_format(train_end_time - train_start_time))

"""
