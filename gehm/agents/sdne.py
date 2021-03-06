import traceback

import torch.nn
from torch.backends import cudnn
from torch.utils.data import DataLoader
#from tqdm import tqdm
from tqdm.autonotebook import tqdm

import numpy as np
import networkx as nx

from gehm.agents.base import BaseAgent
from gehm.datasets.nx_datasets import nx_dataset_sdne, nx_dataset_tsne,batch_nx_dataset_tsne
from gehm.losses.sdne_loss_functions import *
from gehm.model.sdne import SDNEmodel

# from tensorboardX import SummaryWriter
from gehm.utils.measurements import aggregate_measures

cudnn.benchmark = True



class SDNEAgent(BaseAgent):
    def __init__(self, config, G: Union[nx.Graph, nx.DiGraph]):
        super().__init__(config)

        self.config = config

        # set cuda flag
        self.is_cuda = torch.cuda.is_available()
        if self.is_cuda and not self.config.cuda:
            self.logger.info(
                "WARNING: You have a CUDA device, so you should probably enable CUDA"
            )

        self.cuda = self.is_cuda & self.config.cuda
        if self.cuda:
            self.device = "cuda"
        else:
            self.device = "cpu"
        # set the manual seed for torch
        self.manual_seed = self.config.seed
        np.random.seed(self.manual_seed)
        torch.manual_seed(self.manual_seed)

        self.nr_nodes = len(G.nodes)
        self.nr_epochs = config.nr_epochs

        # activation
        if config.activation == "Tanh":
            activation = torch.nn.Tanh
        else:
            activation = torch.nn.Tanh

        # dataset
        self.dataset = nx_dataset_sdne(G)

        # define model
        self.model = SDNEmodel(
            dim_input=self.nr_nodes,
            dim_intermediate=config.dim_intermediate,
            dim_embedding=config.dim_embedding,
            activation=activation,
            nr_encoders=config.nr_encoders,
            nr_decoders=config.nr_decoders,
        )

        # define data_loader
        self.dataloader = DataLoader(
            self.dataset, batch_size=config.batch_size, shuffle=config.shuffle
        )
        self.predict_dataloader = DataLoader(
            self.dataset, batch_size=config.batch_size, shuffle=False
        )
        # define loss
        self.se_loss = SDNESELoss(beta=config.beta1, device=self.device)
        self.pr_loss = SDNEProximityLoss(device=self.device)

        # define optimizers for both generator and discriminator
        self.optimizer = torch.optim.Adam(
            self.model.parameters(),
            lr=config.learning_rate,
            amsgrad=config.amsgrad,
            weight_decay=config.weight_decay,
        )
        self.scheduler = torch.optim.lr_scheduler.StepLR(
            self.optimizer,
            step_size=config.schedule_step_size,
            gamma=config.schedule_gamma,
        )

        # initialize counter
        self.current_epoch = 0
        self.current_iteration = 0

        # dicts
        self.losses_dict = {}
        self.lr_list = []
        self.measures={}

        # Results
        self.positions=None
        self.est_similarity=None

        if self.cuda:
            torch.cuda.manual_seed_all(self.manual_seed)
            torch.cuda.manual_seed(self.manual_seed)
            # torch.cuda.set_device(self.device)
            self.model = self.model.cuda()
            self.se_loss = self.se_loss.cuda()
            self.pr_loss = self.pr_loss.cuda()
            self.logger.info("Program will run on *****GPU-CUDA***** ")
        else:
            self.logger.info("Program will run on *****CPU*****\n")

        # Model Loading from the latest checkpoint if not found start from scratch.
        # self.load_checkpoint(self.config.checkpoint_file)
        # Summary Writer
        # self.summary_writer = None

    def load_checkpoint(self, file_name):
        """
        Latest checkpoint loader
        :param file_name: name of the checkpoint file
        :return:
        """
        pass

    def save_checkpoint(self, file_name="checkpoint.pth.tar", is_best=0):
        """
        Checkpoint saver
        :param file_name: name of the checkpoint file
        :param is_best: boolean flag to indicate whether current checkpoint's accuracy is the best so far
        :return:
        """
        pass

    def run(self):
        """
        The main operator
        :return:
        """
        try:
            self.train()

        except KeyboardInterrupt:
            self.logger.info("You have entered CTRL+C.. Wait to finalize")


    def stack_sample(self, nodes:list, positions:list, similarities:list):
        """
        Simply stacks a sample and orders such that node ids correspond to original sample

        Parameters
        ----------
        nodes: list of numpy arrays
            node id integers
        positions: list of numpy arrays
        similarities: list of numpy arrays

        Returns
        -------
        nodes, positions, similarities, sorted and concatenated
        """

        nodes=np.concatenate(nodes, axis=-1)
        index=np.argsort(nodes)
        nodes=nodes[index]

        positions=np.concatenate(positions, axis=0)
        positions=positions[index,:]

        similarities=np.concatenate(similarities, axis=0)
        similarities=similarities[index,:]

        return nodes,positions,similarities

    def predict(self):
        losses = []
        se_losses = []
        pr_losses = []
        self.model.eval()
        nodes_list=[]
        position_list=[]
        similarity_list=[]
        with torch.no_grad():
            pbar = tqdm(enumerate(self.dataloader), desc="Predicting sample", position=0, leave=False)
            for i, data in pbar:
                node_ids, sim1, sim2 = data
                node_ids = node_ids.to(self.device)
                sim1 = sim1.to(self.device)
                positions, est_sim = self.model(sim1)

                se_loss_value = self.se_loss(est_similarity=est_sim, similarity=sim1)
                pr_loss_value = self.pr_loss(
                    positions=positions, similarity=sim1, indecies=node_ids
                )
                total_loss = se_loss_value + pr_loss_value
                losses.append(total_loss.cpu().detach().numpy())
                se_losses.append(se_loss_value.cpu().detach().numpy())
                pr_losses.append(pr_loss_value.cpu().detach().numpy())

                nodes_list.append(node_ids.cpu().detach().numpy())
                position_list.append(positions.cpu().detach().numpy())
                similarity_list.append(est_sim.cpu().detach().numpy())

        nodes,positions,est_sim = self.stack_sample(nodes_list,position_list,similarity_list)
        self.nodes=nodes
        self.positions=positions
        self.est_similarity = est_sim


        return self.stack_sample(nodes_list,position_list,similarity_list),losses

    def train(self):
        """
        Main training loop
        :return:
        """
        losses = []
        se_losses = []
        pr_losses = []
        lr_list = []
        self.model.train()

        self.current_epoch = 0

        desc = ""
        pbar = tqdm(range(0, self.nr_epochs), desc=desc, position=0, leave=False)
        for epoch in pbar:
            self.current_epoch = epoch
            epoch_loss, se_loss_epoch, pr_loss_epoch = self.train_one_epoch()

            pbar.set_description(
                "Loss: {}, LR:{}".format(epoch_loss, self.scheduler.get_last_lr())
            )

            self.scheduler.step()

            if epoch_loss > 0:
                losses.append(epoch_loss.cpu().detach().numpy())
                se_losses.append(se_loss_epoch.cpu().detach().numpy())
                pr_losses.append(pr_loss_epoch.cpu().detach().numpy())

            lr_list.append(
                self.scheduler.get_last_lr()[0]
                if isinstance(self.scheduler.get_last_lr(), list)
                else self.scheduler.get_last_lr().detach().numpy()
            )

            if epoch * len(self.dataloader.dataset) % self.config.log_interval == 0:
                self.log_loss(epoch_loss)

        self.losses_dict['training_total_loss'] = losses
        self.losses_dict['training_se_loss'] = se_losses
        self.losses_dict['training_pr_loss'] = pr_losses
        self.lr_list = lr_list

        pass

    def draw_losses(self):
        try:
            import pandas as pd
            import matplotlib.pyplot as plt
        except:
            msg="Could not import pandas and matplotlib!"
            logging.error(msg)
            raise ImportError(msg)       
        try:
            #lr_list = pd.DataFrame(np.array(agent.lr_list)[1:])
            #lr_list.plot(title="Learning Rate")
            #plt.show()
            for l in self.losses_dict.keys():
                losses = pd.DataFrame(np.array(self.losses_dict[l])[1:])
                losses.plot(title=l)
                plt.show()
        except:
            msg="Could not draw losses. Please confirm model has been trained! Exception raised: {}".format(traceback.format_exc())
            logging.error(msg)
            raise RuntimeError(msg)

    def draw_embedding(self, node_color_dict:dict=None, node_label_dict:dict=None, xlim:float=None, ylim:float=None):
        try:
            import pandas as pd
            import matplotlib.pyplot as plt
        except:
            msg="Could not import pandas and matplotlib!"
            logging.error(msg)
            raise ImportError(msg)       
        
        try:       
            if self.est_similarity is not None and self.positions is not None and self.nodes is not None:
                est_similarity=torch.as_tensor(self.est_similarity)
                positions=torch.as_tensor(self.positions)
                nodes=torch.as_tensor(self.nodes)
            else:
                logging.info("No positions found in agent, running prediction!")
                predictions,losses = self.predict()
                nodes,positions,est_similarity=predictions
                positions=torch.as_tensor(positions) # just making sure
                est_similarity=torch.as_tensor(est_similarity)
            
            asdf=pd.DataFrame(positions.numpy(), index=nodes.numpy(), columns=["x","y"])

            figure, axes = plt.subplots()
            Drawing_colored_circle = plt.Circle((0, 0), 1, fill=False)

            if xlim is not None:
                plt.xlim(-xlim,xlim)
            if ylim is not None:
                plt.ylim(-ylim, ylim)
            plt.scatter(asdf.x, asdf.y)
            axes.set_aspect(1)
            asdf["drawn"]=0
            for ind in asdf.index:
                row = asdf.loc[ind, :]
                idx = self.dataset.node_idx_dict[ind]
                
                if node_color_dict is not None:
                    try:
                        col=node_color_dict[idx]
                    except:
                        col="blue"
                else:
                    col="blue"
                if node_color_dict is not None:
                    try:
                        node_label=node_label_dict[idx]
                    except:
                        node_label=idx
                else:
                    node_label=idx
                                
                x=row.x
                y=row.y
                close_x=np.where(np.isclose(asdf.x,x,atol=0.1))[0]
                close_y=np.where(np.isclose(asdf.y,y,atol=0.1))[0]
                closeset=np.intersect1d(close_x,close_y)
                neighbors=np.sum(asdf.iloc[closeset,:].drawn)+1
                if neighbors <= 4:
                    npd=np.array([1,1,2,2])
                    nn=neighbors%4
                    xp=np.power(-1,npd[nn-1])*(max(1,neighbors-4))*0.1
                    yp=np.power(-1,nn)*(max(1,neighbors-4))*0.1
                    #print("{}: {} - {},{}".format(ind,neighbors,xp,yp))
                    plt.text(
                        x=row.x + xp,
                        y=row.y + yp,
                        s=node_label,
                        fontdict=dict(color="black", size=5),
                        bbox=dict(facecolor=col, alpha=0.3),
                    )
                asdf.loc[ind, "drawn"]=1

            axes.add_artist(Drawing_colored_circle)
            plt.title("Embedding")
            plt.show()

        except Exception as e:
            msg="Could not draw losses. Exception raised: {}".format(traceback.format_exc())
            logging.error(msg)
            raise RuntimeError(msg)


    def train_one_epoch(self):
        """
        One epoch of training
        :return:
        """

        desc = ""

        epoch_loss = torch.tensor(0)
        se_loss_epoch = torch.tensor(0)
        pr_loss_epoch = torch.tensor(0)

        self.current_iteration = 0

        for i, data in enumerate(self.dataloader):
            self.optimizer.zero_grad()
            node_ids, sim1, sim2 = data
            node_ids = node_ids.to(self.device)
            sim1 = sim1.to(self.device)
            positions, est_sim = self.model(sim1)

            se_loss_value = self.se_loss(est_similarity=est_sim, similarity=sim1)
            pr_loss_value = self.pr_loss(
                positions=positions, similarity=sim1, indecies=node_ids
            )

            total_loss = se_loss_value + pr_loss_value
            total_loss.backward()
            self.optimizer.step()

            epoch_loss += total_loss.cpu().detach().numpy()
            se_loss_epoch += se_loss_value.cpu().detach().numpy()
            pr_loss_epoch += pr_loss_value.cpu().detach().numpy()

            self.current_iteration += 1

        return epoch_loss, se_loss_epoch, pr_loss_epoch

    def validate(self):
        """
        One cycle of model validation
        :return:
        """
        pass

    def measure(self, cut=0.1):

        # Get Predicitions on full data
        predictions,losses = self.predict()
        nodes,positions,similarities=predictions

        self.measures = aggregate_measures(positions=positions, est_similarities=similarities, similarities=self.dataset.sim1)



    def normalize_and_embed(self):
        """
        Finalizes positional embedding by normalizing, reapplying position function and re-measuring deviations.
        :return:
        """
        similarity=self.dataset.sim1.numpy()
        if self.est_similarity is not None and self.positions is not None:
            est_similarity=np.array(self.est_similarity)
            positions=np.array(self.positions)
        else:
            predictions,losses = self.predict()
            nodes,positions,est_similarity=predictions
            positions=np.array(positions) # just making sure
            est_similarity=np.array(est_similarity)

        measure_dict_old=aggregate_measures(positions,est_similarity,similarity)

        logging.info("Normalizing positions with measure {}, re-applying measures".format(self.model.position))

        positions=(positions - np.mean(positions, axis=0)) / np.std(positions, axis=0)

        positions=self.model.position(torch.as_tensor(positions))

        measure_dict_new=aggregate_measures(positions,est_similarity,similarity)

        logging.info("Applied embedding position. Measures as follows:")
        logging.info("emb_map - Old: {}, New: {}".format(measure_dict_old["emb_map"], measure_dict_new["emb_map"]))
        logging.info("emb_l2 - Old: {}, New: {}".format(measure_dict_old["emb_l2"], measure_dict_new["emb_l2"]))
        logging.info("emb_5precision - Old: {}, New: {}".format(measure_dict_old["emb_5precision"], measure_dict_new["emb_5precision"]))


        self.positions=positions


        return positions

