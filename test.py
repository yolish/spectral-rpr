import torch
from datasets.CameraPoseDataset import CameraPoseDataset
from util import utils
import numpy as np
import logging
import time
from util.spectral_sync_utils import calc_relative_poses, decompose_poses_with_exp


def test(model, config, device, dataset_path, train_labels_file, test_labels_file, train_dataset_embedding_path):
    """
    model: (nn.Module) RPR spectral model
    config: config object for set up
    device: (torch.device)
    dataset_path: (str) path to train dataset (where images are located)
    train_labels_file (str) path to a file associating images with their absolute poses - for the train set
    test_labels_file (str) path to a file associating images with their absolute poses - for the test set
    train_dataset_embedding_path (str) path to embedding of the train dataset for knn retrieval
    train_dataset_embedding_path (str) path to embedding of the train dataset for knn retrieval
    return test stats (mean and median pose error and mean inference time)
    """

    # Set to eval mode
    model.eval()

    # Set the dataset and data loader
    transform = utils.test_transforms.get('baseline')
    train_dataset = CameraPoseDataset(dataset_path, train_labels_file, transform)
    test_dataset = CameraPoseDataset(dataset_path, test_labels_file, transform)
    loader_params = {'batch_size': 1,
                     'shuffle': False,
                     'num_workers': config.get('n_workers')}
    dataloader = torch.utils.data.DataLoader(test_dataset, **loader_params)
    train_dataset_embedding = torch.load(train_dataset_embedding_path)
    train_poses = train_dataset.poses
    k = config.get("k")

    stats = np.zeros((len(dataloader.dataset), 3))
    with torch.no_grad():
        for i, minibatch in enumerate(dataloader, 0):
            for key, value in minibatch.items():
                minibatch[key] = value.to(device)

            # TODO consider using NetVLAD for IR purposes
            # Forward pass to predict the pose
            tic = time.time()
            # embed the image
            latent_query = model.forward_backbone(minibatch.get('img'))

            # Fetch latent of knns
            knn_indices = utils.fetch_knn(latent_query, train_dataset_embedding, k)
            latent_knns = train_dataset_embedding[knn_indices, :].unsqueeze(0)

            # Get relative poses from knns to query
            rel_query_ts, rel_query_quats = model.forward_regressor_heads(latent_query.repeat((latent_knns.shape[0], 1)),
                                                                        latent_knns)

            #TODO add error of relative

            # Get relative poses
            knn_poses = train_poses[knn_indices, :]
            knn_rel_exp_ts, knn_rel_rots = calc_relative_poses(knn_poses)

            # Prepare GT abs t and R for finding the coefficients of the linear combination of the eigen vectors
            exp_abs_knn_ts, abs_knn_rots = decompose_poses_with_exp(knn_poses)

            abs_t, abs_rot = model.foward_spectral(knn_rel_exp_ts, knn_rel_rots,
                                                exp_abs_knn_ts, abs_knn_rots,
                                                rel_query_ts, rel_query_quats)

            # get absolute pose using spectral analysis
            est_pose = torch.cat((abs_t, abs_rot), dim=1)
            toc = time.time()
            gt_pose = minibatch.get('pose').to(dtype=torch.float32)

            # Evaluate error
            posit_err, orient_err = utils.pose_err(est_pose, gt_pose)

            # Collect statistics
            stats[i, 0] = posit_err.item()
            stats[i, 1] = orient_err.item()
            stats[i, 2] = (toc - tic) * 1000

            # Record
            logging.info("Pose error: {:.3f}[m], {:.3f}[deg], inferred in {:.2f}[ms]".format(
                stats[i, 0], stats[i, 1], stats[i, 2]))

    return stats

