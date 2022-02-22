#!/bin/bash
#SBATCH --job-name=all_learnlet_512    # nom du job
##SBATCH --partition=gpu_p2          # de-commente pour la partition gpu_p2
#SBATCH --ntasks=1                   # nombre total de tache MPI (= nombre total de GPU)
#SBATCH --ntasks-per-node=1          # nombre de tache MPI par noeud (= nombre de GPU par noeud)
#SBATCH --gres=gpu:1                 # nombre de GPU par n?~Sud (max 8 avec gpu_p2)
#SBATCH --cpus-per-task=10           # nombre de coeurs CPU par tache (un quart du noeud ici)
#SBATCH -C v100-32g 
# /!\ Attention, "multithread" fait reference a l'hyperthreading dans la terminologie Slurm
#SBATCH --hint=nomultithread         # hyperthreading desactive
#SBATCH --time=72:00:00              # temps d'execution maximum demande (HH:MM:SS)
#SBATCH --output=all_learnlet_512_%j.out  # nom du fichier de sortie
#SBATCH --error=all_learnlet_512_%j.err   # nom du fichier d'erreur (ici commun avec la sortie)
#SBATCH -A ynx@gpu                   # specify the project
##SBATCH --qos=qos_gpu-dev            # using the dev queue, as this is only for profiling

# nettoyage des modules charges en interactif et herites par defaut
module purge

# chargement des modules
module load tensorflow-gpu/py3/2.4.1

# echo des commandes lancees
set -x

cd $WORK/repo/deep_mccd/scripts/

srun python -u training_learnlets.py \
    --run_id_name all_learnlet_512 \
    --dataset_path /gpfswork/rech/ynx/ulx23va/deep_mccd_project/eigenpsf_datasets/all_eigenpsfs.fits \
    --base_save_path /gpfswork/rech/ynx/ulx23va/deep_mccd_project/trained_models/all_learnlet_512/ \
    --n_tiling 512 \
    --n_scales 5 \
    --use_lr_scheduler True \
    --enhance_noise True \
    --n_shuffle 50 \
    --batch_size 64 \
    --n_epochs 500 \
    --lr_param 1e-3 \
    --data_train_ratio 0.9 \
    --snr_range 1e-3 100 \

