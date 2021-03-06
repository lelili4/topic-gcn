# CGAT: Channel-aware Graph Attention Networks
This repo is for paper: [Graph Attention Networks over Edge Content-Based Channels](https://www.kdd.org/kdd2020/accepted-papers/view/graph-attention-networks-over-edge-content-based-channels), [Lu Lin](https://louise-lulin.github.io/) and [Hongning Wang](http://www.cs.virginia.edu/~hw5x/). KDD2020. 

## Updates
* [2020.08] Upload sampled yelp and stackoverflow data
* [2020.03] Upload model codes

## Requirements
* Python=3.6
* tensorflow-gpu=1.14
* networkx
* Linux: cuda-toolkit-10, cudnn-7.5.0

## Run the Code
### Datasets
Sampled Yelp and StackOverflow under folder `./data`

### Demo
`python run_unsupervised.py --training-data-dir $training_dataset_folder --embed-dir $embedding_save_folder`

## Cite
Welcome to try and cite:
```
@inproceedings{lin2020graph,
  title={Graph Attention Networks over Edge Content-Based Channels},
  author={Lin, Lu and Wang, Hongning},
  booktitle={Proceedings of the 26th ACM SIGKDD International Conference on Knowledge Discovery \& Data Mining},
  pages={1819--1827},
  year={2020}
}
```
## Acknowledgement
The code is partially modified from [williamleif/GraphSAGE](https://github.com/williamleif/GraphSAGE)

