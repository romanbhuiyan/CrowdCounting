from data_reader import DataReader
import matplotlib
from matplotlib import pyplot as plt
import cv2
import numpy as np
import os
import random, string
import math
import pickle


PRED_DOWNSCALE_FACTORS = (8, 4, 2, 1)

# Size increments for the box sizes (\gamma) as mentioned in the paper.
GAMMA = [1, 1, 2, 4]

# Number of predefined boxes per scales (n_{mathcal{B}}).
NUM_BOXES_PER_SCALE = 3
###############################################################

# ---- Computing predefined box sizes and global variables
BOX_SIZE_BINS = [1]
BOX_IDX = [0]
g_idx = 0
while len(BOX_SIZE_BINS) < NUM_BOXES_PER_SCALE * len(PRED_DOWNSCALE_FACTORS):
    gamma_idx = len(BOX_SIZE_BINS) // (len(GAMMA)-1)
    box_size = BOX_SIZE_BINS[g_idx] + GAMMA[gamma_idx]
    box_idx = gamma_idx*(NUM_BOXES_PER_SCALE+1) + (len(BOX_SIZE_BINS) % (len(GAMMA)-1))
    BOX_IDX.append(box_idx)
    BOX_SIZE_BINS.append(box_size)
    g_idx += 1
BOX_INDEX = dict(zip(BOX_SIZE_BINS, BOX_IDX))
SCALE_BINS_ON_BOX_SIZE_BINS = [NUM_BOXES_PER_SCALE * (s + 1) \
                               for s in range(len(GAMMA))]
BOX_SIZE_BINS_NPY = np.array(BOX_SIZE_BINS)
BOXES = np.reshape(BOX_SIZE_BINS_NPY, (4, 3))
BOXES = BOXES[::-1]
metrics = ['loss1', 'new_mae']

# Loss Weights (to be read from .npy file while training)
loss_weights = None


matplotlib.use('Agg')
parser = argparse.ArgumentParser(description='Training')
parser.add_argument('--epochs', default=50, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--gpu', default=1, type=int,
                    help='GPU number')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N', 
                    help='manual epoch number (useful on restarts),\
                    0-indexed - so equal to the number of epochs completed \
                    in the last save-file')
parser.add_argument('-b', '--batch-size', default=4, type=int, metavar='N',
                    help='mini-batch size (default: 4),only used for train')

parser.add_argument('--trained-model', default='', type=str, metavar='PATH', help='filename of model to load', nargs='+')
dataset_paths, model_save_dir, batch_size, crop_size, dataset = None, None, None, None, None


class networkFunctions():

    def __init__(self):
        self.train_funcs = []
        self.test_funcs = None
        self.optimizers = None

    def get_box_gt(self, Yss):
        Yss_out = []
        for yss in Yss:  # iterate over all scales!
            # Make empty maps of shape gt_pred_map.shape for x, y, w, h
            w_map = np.zeros((yss.shape[0], 4) + yss.shape[2:])  # (B,4,h,w)
            w_map[:, 3] = 1  # Making Z initialized as 1's since they are in majority!
            Yss_out.append(w_map)
        assert(len(Yss_out) == 4)
        # Get largest spatial gt
        yss_np = Yss[0].cpu().data.numpy()
        gt_ref_map = yss_np  # (B, 1, h, w)
        # For every gt patch from the gt_ref_map
        for b in range(0, gt_ref_map.shape[0]):
            y_idx, x_idx = np.where(gt_ref_map[b][0] > 0)
            num_heads = y_idx.shape[0]
            if num_heads > 1:
                distances = (x_idx - x_idx[np.newaxis, :].T) ** 2 + (y_idx - y_idx[np.newaxis, :].T) ** 2
                min_distances = np.sqrt(np.partition(distances, 1, axis=1)[:, 1])
                min_distances = np.minimum(min_distances, np.inf) ##? WHY INF???
                box_inds = np.digitize(min_distances, BOX_SIZE_BINS_NPY, False)
                box_inds = np.maximum(box_inds - 1, 0) # to make zero based indexing
            elif num_heads == 1:
                box_inds = np.array([BOX_SIZE_BINS_NPY.shape[0] - 1])
            else:
                box_inds = np.array([])
            assert(np.all(box_inds < BOX_SIZE_BINS_NPY.shape[0]))
            scale_inds = np.digitize(box_inds, SCALE_BINS_ON_BOX_SIZE_BINS, False)
            # Assign the w_maps
            check_sum = 0
            for i, (yss, w_map) in enumerate(zip(Yss, Yss_out)):
                scale_sel_inds = (scale_inds == i)

                check_sum += np.sum(scale_sel_inds)

                if scale_sel_inds.shape[0] > 0:
                    # find box index in the scale
                    sel_box_inds = box_inds[scale_sel_inds]
                    scale_box_inds = sel_box_inds % 3 
                    heads_y = y_idx[scale_sel_inds] // PRED_DOWNSCALE_FACTORS[3-i]
                    heads_x = x_idx[scale_sel_inds] // PRED_DOWNSCALE_FACTORS[3-i]
                    
                    Yss_out[i][b, scale_box_inds, heads_y, heads_x] = BOX_SIZE_BINS_NPY[sel_box_inds]
                    Yss_out[i][b, 3, heads_y, heads_x] = 0

            assert(check_sum == torch.sum(Yss[0][b]).item() == len(y_idx))
            
        Yss_out = [torch.cuda.FloatTensor(w_map) for w_map in Yss_out]
        check_sum = 0
        for yss_out in Yss_out:
            yss_out_argmax, _ = torch.max(yss_out[:, 0:3], dim=1)
            yss_out_argmax = (yss_out_argmax>0).type(torch.cuda.FloatTensor)
            check_sum += torch.sum(yss_out_argmax).item()

        yss = (Yss[0]>0).type(torch.cuda.FloatTensor)
        assert(torch.sum(yss) == check_sum)

        return Yss_out


    def upsample_single(self, input_, factor=2):
        channels = input_.size(1)
        indices = torch.nonzero(input_)
        indices_up = indices.clone()
        # Corner case! 
        if indices_up.size(0) == 0:
            return torch.zeros(input_.size(0),input_.size(1), input_.size(2)*factor, input_.size(3)*factor).cuda()
        indices_up[:, 2] *= factor
        indices_up[:, 3] *= factor
        
        output = torch.zeros(input_.size(0),input_.size(1), input_.size(2)*factor, input_.size(3)*factor).cuda()
        output[indices_up[:, 0], indices_up[:, 1], indices_up[:, 2], indices_up[:, 3]] = input_[indices[:, 0], indices[:, 1], indices[:, 2], indices[:, 3]]

        output[indices_up[:, 0], channels-1, indices_up[:, 2]+1, indices_up[:, 3]] = 1.0
        output[indices_up[:, 0], channels-1, indices_up[:, 2], indices_up[:, 3]+1] = 1.0
        output[indices_up[:, 0], channels-1, indices_up[:, 2]+1, indices_up[:, 3]+1] = 1.0


        output_check = nn.functional.max_pool2d(output, kernel_size=2)

        return output


    def gwta_loss(self, pred, gt, criterion, grid_factor=2):
        patch_size_h = int((pred.size(2) / grid_factor).item())
        patch_size_w = int((pred.size(3) / grid_factor).item())

        pred_re = pred.unfold(2, patch_size_h, patch_size_h).unfold(3, patch_size_w, patch_size_w).contiguous()
        gt_re = gt.unfold(1, patch_size_h, patch_size_h).unfold(2, patch_size_w, patch_size_w).contiguous()

        pred_re_merged = pred_re.view(pred_re.size(0), pred_re.size(1), -1, pred_re.size(-2), pred_re.size(-1))
        gt_re_merged = gt_re.view(gt_re.size(0), -1, gt_re.size(-2), gt_re.size(-1))

        grids_in_each_column = int(pred.shape[2] / patch_size_h)
        grids_in_each_row = int(pred.shape[3] / patch_size_w)
        num_grids = grids_in_each_column * grids_in_each_row

        assert(num_grids == pred_re_merged.size(2))
        assert(num_grids == gt_re_merged.size(1))

        max_loss = -float("inf")
        for ng in range(num_grids):
            out = pred_re_merged[:, :, ng]
            yss = gt_re_merged[:, ng]
            curr_loss = criterion(out, yss)
            if curr_loss > max_loss:
                max_loss = curr_loss
        return max_loss    

    def create_network_functions(self, network):
        self.optimizers = optim.SGD(filter(lambda p: p.requires_grad, network.parameters()),
                                         lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
        
        def train_function(Xs, Ys, hist_boxes, hist_boxes_gt, loss_weights, network):
            Ys = (Ys>0).astype(np.float32)
            network = network.cuda()
            self.optimizers.zero_grad()

            if torch.cuda.is_available():
                X = torch.autograd.Variable(torch.from_numpy(Xs)).cuda()
                
                Y = torch.autograd.Variable(torch.FloatTensor(Ys)).cuda()
                Yss = [Y]
            else:
                assert(0)
            for s in range(0, 3):
                Yss.append(torch.nn.functional.avg_pool2d(Yss[s], (2, 2)) * 4)
            
            output_vars = [network(X, None)]


            outputs_1 = [out for out in output_vars[0]]
            
            Yss_out = self.get_box_gt(Yss) # Making 4 channel ground truth
            Yss = Yss[::-1]        # Reverse GT for uniformity of having lowest scale in the beginning
            Yss_out = Yss_out[::-1]    # Reverse pred for uniformity of having lowest scale in the beginning

            # Put outputs in list
            outputs = [out for out in output_vars[0]]

            losses = []
            sums = []

            Yss_argmax = [torch.argmax(yss, dim=1) for yss in Yss_out]
        
            alpha1 = torch.cuda.FloatTensor(loss_weights[3])  # 1/16 scale
            alpha2 = torch.cuda.FloatTensor(loss_weights[2])  # 1/8 scale
            alpha3 = torch.cuda.FloatTensor(loss_weights[1])  # 1/4 scale
            alpha4 = torch.cuda.FloatTensor(loss_weights[0])  # 1/2 scale

            m_1 = nn.CrossEntropyLoss(size_average=True, weight=alpha1)
            m_2 = nn.CrossEntropyLoss(size_average=True, weight=alpha2)
            m_3 = nn.CrossEntropyLoss(size_average=True, weight=alpha3)
            m_4 = nn.CrossEntropyLoss(size_average=True, weight=alpha4)
            
            loss = 0.0

            for idx, (m, out, yss) in enumerate(zip([m_1, m_2, m_3, m_4], outputs, Yss_argmax)):
                if idx != 0:
                    loss_ = self.gwta_loss(out, yss, m, grid_factor=np.power(2, idx))
                else:
                    loss_ = m(out, yss)
                loss += loss_
                losses.append(loss_.item())
                
            loss.backward()
            self.optimizers.step()
            
            # -- Histogram of boxes for weighting -- 
            for out_idx, (out, yss) in enumerate(zip(outputs[::-1], Yss_out[::-1])):
                out_argmax = torch.argmax(out, dim=1)
                bin_ = np.bincount(out_argmax.cpu().data.numpy().flatten())
                ii = np.nonzero(bin_)[0]
                hist_boxes[ii+4*out_idx] += bin_[ii]
                
                Yss_argmax = torch.argmax(yss, dim=1)
                bin_gt = np.bincount(Yss_argmax.cpu().data.numpy().flatten())
                ii_gt = np.nonzero(bin_gt)[0]
                hist_boxes_gt[ii_gt+4*out_idx] += bin_gt[ii_gt]


            return losses, hist_boxes, hist_boxes_gt

        def test_function(X, Y, loss_weights, network):
            Y = (Y>0).astype(np.float32)
            if torch.cuda.is_available():
                X = torch.autograd.Variable(torch.from_numpy(X)).cuda()
                X_clone = X.clone()
                Y = torch.autograd.Variable(torch.from_numpy(Y)).cuda()
                Yss = [Y]
            else:
                assert(0)
            
            network = network.cuda()
            output = network(X, None)

            for s in range(0, 3):
                Yss.append(torch.nn.functional.avg_pool2d(Yss[s], (2, 2)) * 4)
            
            assert(torch.sum(Yss[0]) == torch.sum(Yss[1]))

            # Making 4 channel ground truth
            Yss_out = self.get_box_gt(Yss)

            Yss = Yss[::-1]
            Yss_out = Yss_out[::-1]

            Yss_argmax = [torch.argmax(yss, dim=1) for yss in Yss_out]
            alpha1 = torch.cuda.FloatTensor(loss_weights[3])  # 1/16 scale
            alpha2 = torch.cuda.FloatTensor(loss_weights[2])  # 1/8 scale
            alpha3 = torch.cuda.FloatTensor(loss_weights[1])  # 1/4 scale
            alpha4 = torch.cuda.FloatTensor(loss_weights[0])  # 1/2 scale

def load_model_VGG16(net, dont_load=[]):
    if 'scale_4' in net.name:
        cfg = OrderedDict()
        cfg['conv1_1'] = 0
        cfg['conv1_2'] = 2
        cfg['conv2_1'] = 5
        cfg['conv2_2'] = 7
        cfg['conv3_1'] = 10
        cfg['conv3_2'] = 12
        cfg['conv3_3'] = 14
        cfg['conv4_1'] = 17
        cfg['conv4_2'] = 19
        cfg['conv4_3'] = 22
        cfg['conv5_1'] = 22
        cfg['conv5_2'] = 22
        cfg['conv5_3'] = 22
        cfg['conv_middle_1'] = 'conv4_1'
        cfg['conv_middle_2'] = 'conv4_2'
        cfg['conv_middle_3'] = 'conv4_3'
        cfg['conv_lowest_1'] = 'conv3_1'
        cfg['conv_lowest_2'] = 'conv3_2'
        cfg['conv_lowest_3'] = 'conv3_3'
        cfg['conv_scale1_1'] = 'conv2_1'
        cfg['conv_scale1_2'] = 'conv2_2'

        print ('model loading ', net.name)
        base_dir = "../vgg_w/"
        layer_copy_count = 0
        for layer in cfg.keys():
            if layer in dont_load:
                print (layer, 'skipped.')
                continue
            print ("Copying ", layer)
            
            for name, module in net.named_children():
                if layer == name and (not layer.startswith("conv_middle_")) and (not layer.startswith("conv_lowest_") and (not layer.startswith("conv_scale1_"))):
                    lyr = module
                    W = np.load(base_dir + layer + "W.npy")
                    b = np.load(base_dir + layer + "b.npy")
                    lyr.weight.data.copy_(torch.from_numpy(W))
                    lyr.bias.data.copy_(torch.from_numpy(b))
                    layer_copy_count += 1
                elif (layer.startswith("conv_middle_") or layer.startswith("conv_lowest_") or layer.startswith("conv_scale1_")) and name == layer:
                    lyr = module
                    W = np.load(base_dir + cfg[layer] + "W.npy")
                    b = np.load(base_dir + cfg[layer] + "b.npy")
                    lyr.weight.data.copy_(torch.from_numpy(W))
                    lyr.bias.data.copy_(torch.from_numpy(b))
                    layer_copy_count += 1

        print(layer_copy_count, "Copy count")
        assert layer_copy_count == 21
        print ('Done.')


def get_offset_error(x_pred, y_pred, x_true, y_true, output_downscale, max_dist=16):
    if max_dist is None:
        max_dist = 16
    n = len(x_true)
    m = len(x_pred)
    if m == 0 or n == 0:
        return 0
        
    x_true *= output_downscale
    y_true *= output_downscale
    x_pred *= output_downscale
    y_pred *= output_downscale
    dx = np.expand_dims(x_true, 1) - x_pred
    dy = np.expand_dims(y_true, 1) - y_pred
    d = np.sqrt(dx ** 2 + dy ** 2)
    assert d.shape == (n, m)
    sorted_idx = np.asarray(np.unravel_index(np.argsort(d.ravel()), d.shape))
    # Need to divide by n for average error
    hit_thresholds = np.arange(12, -1, -1)
    off_err, num_hits, fn = offset_sum(sorted_idx, d, n, m, max_dist, hit_thresholds, len(hit_thresholds))
    off_err /= n
    precisions = np.asarray(num_hits, dtype='float32') / m
    recall = np.asarray(num_hits, dtype='float32') / ( np.asarray(num_hits, dtype='float32') +  np.asarray(fn, dtype='float32'))
    avg_precision = precisions.mean()
    avg_recall = recall.mean()
    return off_err, avg_precision, avg_recall


def get_boxed_img(image, h_map, w_map, gt_pred_map, prediction_downscale, thickness=1, multi_colours=False):
    if multi_colours:
        colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255)] # colours for [1/8, 1/4, 1/2] scales

    if image.shape[2] != 3:
        boxed_img = image.astype(np.uint8).transpose((1, 2, 0)).copy()
    else:
        boxed_img = image.astype(np.uint8).copy()
    head_idx = np.where(gt_pred_map > 0)

    H, W = boxed_img.shape[:2]

    Y, X = head_idx[-2] , head_idx[-1]
    for y, x in zip(Y, X):

        h, w = h_map[y, x]*prediction_downscale, w_map[y, x]*prediction_downscale

        if multi_colours:
            selected_colour = colours[(BOX_SIZE_BINS.index(h // prediction_downscale)) // 3]
        else:
            selected_colour = (0, 255, 0)
        if h//2 in BOXES[3] or h//2 in BOXES[2]:
            t = 1
        else:
            t = thickness
        cv2.rectangle(boxed_img, (max(int(prediction_downscale * x - w / 2), 0), max(int(prediction_downscale * y - h / 2), 0)),
                      (min(int(prediction_downscale * x + w - w / 2), W), min(int(prediction_downscale * y + h - h / 2), H)), selected_colour, t)
    return boxed_img.transpose((2, 0, 1))


def test_lsccnn(test_funcs, dataset, set_name, network, print_output=False, thresh=0.2):
    test_functions = []
    global test_loss
    global counter
    test_loss = 0.
    counter = 0.
    metrics_test = {}
    metrics_ = ['new_mae', 'mle', 'mse', 'loss1']
    for k in metrics_:
        metrics_test[k] = 0.0
    
    global loss_weights
    if loss_weights is None:
        loss_weights = np.ones((len(PRED_DOWNSCALE_FACTORS), NUM_BOXES_PER_SCALE+1))
    def test_function(img_batch, gt_batch, roi_batch):
        global test_loss
        global counter
        gt_batch = (gt_batch > 0).astype(np.float32)
        loss, pred_batch, gt_batch = test_funcs(img_batch, gt_batch, loss_weights, network)
        test_loss += loss
        counter += 1
        return (*pred_batch), (*gt_batch)

    if isinstance(print_output, str):
        print_path = print_output
    elif isinstance(print_output, bool) and print_output:
        print_path = './'
    else:
        print_path = None

    e = dataset.iterate_over_test_data(test_function, set_name)

    for e_idx, e_iter in enumerate(e):
        image_split = e_iter[1].split('/')
        image_name = image_split[len(image_split)-1]
        image = cv2.imread(e_iter[1])
        maps = [(image, {}),
                (e_iter[2], {'cmap': 'jet', 'vmin': 0., 'vmax': 1.})]

        pred_dot_map, pred_box_map = get_box_and_dot_maps(e_iter[0][0:4], thresh=thresh) # prediction_downscale

        # -- Plotting boxes
        boxed_image_pred = get_boxed_img(image, pred_box_map, pred_box_map, \
                                    pred_dot_map, prediction_downscale=2, \
                                    thickness=2, multi_colours=False)
        boxed_image_pred_path = os.path.join(print_path, image_name + '_boxed_image.png')
        cv2.imwrite(boxed_image_pred_path, boxed_image_pred.astype(np.uint8).transpose((1, 2, 0)))
        print_graph(maps, "", os.path.join(print_path, image_name))

        # -- Calculate metrics
        metrics_test = calculate_metrics(pred_dot_map, e_iter[2], metrics_test)
        
    for m in metrics_:
        metrics_test[m] /= float(e_idx+1)
    metrics_test['mse'] = np.sqrt(metrics_test['mse'])
    metrics_test['loss1'] = test_loss / float(counter)
    txt = ''
    for metric in metrics_test.keys():
        if metric == "mle" and (args.mle == False):
            continue
        txt += '%s: %s ' % (metric, metrics_test[metric])

    return metrics_test, txt


def calculate_metrics(pred, true, metrics_test):
    pred_count = np.sum(pred)
    true_count = np.sum(true)
    head_x_true, head_y_true = np.where(pred > 0)[-2:]
    head_x_pred, head_y_pred = np.where(true > 0)[-2:]
    if args.mle:
        if len(head_x_pred) == 0:
            off = 16*len(head_y_pred)
        else:
            off, _, _ = get_offset_error(head_x_pred, head_y_pred, head_x_true, head_y_true, output_downscale)
        metrics_test['mle'] +=  off
    metrics_test['new_mae'] += np.abs(true_count - pred_count)
    metrics_test['mse'] += (true_count - pred_count) ** 2

    return metrics_test


def find_class_threshold(f, dataset, iters, test_funcs, network, splits=10, beg=0.0, end=0.3):
    for li_idx in range(iters):
        avg_errors = []
        threshold = list(np.arange(beg, end, (end - beg) / splits))
        log(f, 'threshold:'+str(threshold))
        for class_threshold in threshold:
            avg_error = test_lsccnn(test_funcs, dataset, 'test_valid', network, True, thresh=class_threshold)
            avg_errors.append(avg_error[0]['new_mae'])
            log(f, "class threshold: %f, avg_error: %f" % (class_threshold, avg_error[0]['new_mae']))

        mid = np.asarray(avg_errors).argmin()
        beg = threshold[max(mid - 2, 0)]
        end = threshold[min(mid + 2, splits - 1)]
    log(f, "Best threshold: %f" % threshold[mid])
    optimal_threshold = threshold[mid]
    return optimal_threshold

def box_NMS(predictions, thresh):
    Scores = []
    Boxes = []
    for k in range(len(BOXES)):
        scores = np.max(predictions[k], axis=0)
        boxes = np.argmax(predictions[k], axis=0)
        # index the boxes with BOXES to get h_map and w_map (both are the same for us)
        mask = (boxes<3) # removing Z
        boxes = (boxes+1) * mask
        scores = (scores * mask) # + 100 # added 100 since we take logsoftmax and it's negative!! 
    
        boxes = (boxes==1)*BOXES[k][0] + (boxes==2)*BOXES[k][1] + (boxes==3)*BOXES[k][2]
        Scores.append(scores)
        Boxes.append(boxes)

    x, y, h, w, scores = apply_nms.apply_nms(Scores, Boxes, Boxes, 0.5, thresh=thresh)
    
    nms_out = np.zeros((predictions[0].shape[1], predictions[0].shape[2])) # since predictions[0] is of size 4 x H x W
    box_out = np.zeros((predictions[0].shape[1], predictions[0].shape[2])) # since predictions[0] is of size 4 x H x W
    for (xx, yy, hh) in zip(x, y, h):
        nms_out[yy, xx] = 1
        box_out[yy, xx] = hh
    
    assert(np.count_nonzero(nms_out) == len(x))

    return nms_out, box_out


def train_networks(network, dataset, network_functions, log_path):
    snapshot_path = os.path.join(log_path, 'snapshots')
    f = open(os.path.join(log_path, 'train0.log'), 'w')

    # -- Logging Parameters
    log(f, 'args: ' + str(args))
    log(f, 'model: ' + str(network), False)
    log(f, 'Training0...')
    log(f, 'LR: %.12f.' % (args.lr))
    log(f, 'Classification Model')

    # -- Get train, test functions
    train_funcs, test_funcs = network_functions.create_network_functions(network)
    if start_epoch > 0:
        with open(os.path.join(snapshot_path, 'losses.pkl'), 'rb') as lossfile:
            train_losses, valid_losses, test_losses = pickle.load(lossfile, encoding='latin1')
        print ('loaded prev losses')
        for metric in metrics:
            try:
                valid_losses[metric] = valid_losses[metric][:start_epoch]
            except:
                pass
            test_losses[metric] = test_losses[metric][:start_epoch]
        for metric in train_losses.keys():
            train_losses[metric] = train_losses[metric][:start_epoch]
        
        network, _= load_net(network,
                             network_functions, 0,
                             snapshot_path, 
                             get_filename(\
                             network.name,
                             start_epoch))

    # -- Main Training Loop
    global loss_weights
    if os.path.isfile("loss_w.npy"):
        loss_weights = np.load('loss_w.npy')
    else:
        loss_weights = np.ones((4, 4))
    HIST_GT = []
    for e_i, epoch in enumerate(range(start_epoch, num_epochs)):
        avg_loss = [0.0 for _ in range(1)]
        hist_boxes = np.zeros((16,))
        hist_boxes_gt = np.zeros((16,))
        
        # b_i - batch index
        for b_i in range(num_batches_per_epoch):
            # Generate next training sample
            Xs, Ys, _ = dataset.train_get_batch()
            losses, hist_boxes, hist_boxes_gt = train_funcs[0](Xs, Ys, hist_boxes, hist_boxes_gt, loss_weights, network)


            for scale_idx in range(1):
                avg_loss[scale_idx] = avg_loss[scale_idx] + losses[scale_idx]
            
            # Logging losses after 1k iterations.
            if b_i % 1000 == 0:
                log(f, 'Epoch %d [%d]: %s loss: %s.' % (epoch, b_i, [network.name], losses))
                log(f, 'hist_boxes %s.' % (np.array_str(np.int32(hist_boxes))))
                log(f, 'hist_boxes_gt %s.' % (np.array_str(np.int32(hist_boxes_gt))))
                hist_boxes = np.zeros((16,))
                hist_boxes_gt = np.zeros((16,))
                HIST_GT.append(hist_boxes_gt)
        if np.all(loss_weights == 1):
            HIST_GT = np.asarray(HIST_GT)
            HIST_GT = np.sum(HIST_GT, axis=0)
            HIST_GT = np.reshape(HIST_GT, (4, 4))
            loss_weights = compute_box_weights(HIST_GT)
            np.save('loss_weights.npy', loss_weights)
            print("Saving loss weights!! PLEASE re-run the code for training/testing")
            exit()
        # Save networks
        save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': network.state_dict(),
                'optimizer': network_functions.optimizers.state_dict(),
            }, snapshot_path, get_filename(network.name, epoch + 1))

        for metric in train_losses.keys():
            if "maxima_split" not in metric:
                if isinstance(train_losses[metric][0], list):
                    for i in range(len(train_losses[metric][0])):
                        plt.plot([a[i] for a in train_losses[metric]])
                        plt.savefig(os.path.join(snapshot_path, 'train_%s_%d.png' % (metric, i)))
                        plt.clf()
                        plt.close()
                print(metric, "METRIC", train_losses[metric])
                plt.plot(train_losses[metric])
                plt.savefig(os.path.join(snapshot_path, 'train_%s.png' % metric))
                plt.clf()
                plt.close()
        
        for metric in valid_losses.keys():
            if isinstance(valid_losses[metric][0], list):
                for i in range(len(valid_losses[metric][0])):
                    plt.plot([a[i] for a in valid_losses[metric]])
                    plt.savefig(os.path.join(snapshot_path, 'valid_%s_%d.png' % (metric, i)))
                    plt.clf()
                    plt.close()
            plt.plot(valid_losses[metric])
            plt.savefig(os.path.join(snapshot_path, 'valid_%s.png' % metric))
            plt.clf()
            plt.close()
        
        for metric in test_losses.keys():
            if isinstance(test_losses[metric][0], list):
                for i in range(len(test_losses[metric][0])):
                    plt.plot([a[i] for a in test_losses[metric]])
                    plt.savefig(os.path.join(snapshot_path, 'test_%s_%d.png' % (metric, i)))
                    plt.clf()
                    plt.close()
            plt.plot(test_losses[metric])
            plt.savefig(os.path.join(snapshot_path, 'test_%s.png' % metric))
            plt.clf()
            plt.close()

  

    model_save_path = os.path.join(model_save_dir, 'train2')
    if not os.path.exists(model_save_path):
        os.makedirs(model_save_path)
        os.makedirs(os.path.join(model_save_path, 'snapshots'))

    train_networks(network=network, 
                   dataset=dataset, 
                   network_functions=networkFunctions(),
                   log_path=model_save_path)

    print('\n-------\nDONE.')

if __name__ == '__main__':
    args = parser.parse_args()    
    # -- Assign GPU    
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)

    # -- Assertions
    assert(args.dataset)
    assert len(args.trained_model) in [0, 1]

    # -- Setting seeds for reproducability
    np.random.seed(11)
    random.seed(11)
    torch.manual_seed(11)
    torch.backends.cudnn.enabled = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.cuda.manual_seed(11)
    torch.cuda.manual_seed_all(11)

    # -- Dataset paths
    if args.dataset == "parta":    
        dataset_paths = {'test': ['../dataset/Hajj-Crowd/test_data/images']    
                         'train': ['../dataset/Hajj-Crowd/train_data/images']}
        validation_set = 30

        path = '../dataset/hajj-crowd_predscale0.5_rgb_ddcnn++_test_val_30'
        output_downscale = 2
    elif args.dataset == "partb":
        dataset_paths = {'test': ['../dataset/ucfcc50/test_data/images']
                         'train': ['../dataset/ucfcc50/train_data/images']}
        validation_set = 80
        output_downscale = 2
        path = '../dataset/ucc50_dotmaps_predictionScale_'+str(output_downscale)

    
    model_save_dir = './models'

    batch_size = args.batch_size
    crop_size = 224
    dataset = DataReader(path)
    
    # -- Train the model
    train()

