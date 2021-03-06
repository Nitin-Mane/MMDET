import torch.nn as nn
import torch.nn.functional as F
import torch
from mmcv.cnn import xavier_init

from ..plugins import NonLocal2D
from ..registry import NECKS
from ..utils import ConvModule


@NECKS.register_module
class BFP(nn.Module):
    """BFP (Balanced Feature Pyrmamids)

    BFP takes multi-level features as inputs and gather them into a single one,
    then refine the gathered feature and scatter the refined results to
    multi-level features. This module is used in Libra R-CNN (CVPR 2019), see
    https://arxiv.org/pdf/1904.02701.pdf for details.

    Args:
        in_channels (int): Number of input channels (feature maps of all levels
            should have the same channels).
        num_levels (int): Number of input feature levels.
        conv_cfg (dict): The config dict for convolution layers.
        norm_cfg (dict): The config dict for normalization layers.
        refine_level (int): Index of integration and refine level of BSF in
            multi-level features from bottom to top.
        refine_type (str): Type of the refine op, currently support
            [None, 'conv', 'non_local'].
    """

    def __init__(self,
                 in_channels,
                 num_levels,
                 refine_level=2,
                 refine_type=None,
                 output_single_lvl=False,
                 conv_cfg=None,
                 norm_cfg=None,
                 out_channels=None):
        super(BFP, self).__init__()
        assert refine_type in [None, 'conv', 'non_local']

        self.in_channels = in_channels
        self.num_levels = num_levels
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.output_single_lvl = output_single_lvl

        self.refine_level = refine_level
        self.refine_type = refine_type
        self.out_channels = out_channels
        assert 0 <= self.refine_level < self.num_levels

        if self.output_single_lvl:
            self.refine = ConvModule(
                sum(self.in_channels),
                self.out_channels,
                3,
                padding=1,
                conv_cfg=self.conv_cfg,
                norm_cfg=self.norm_cfg)
        elif self.refine_type == 'conv':
            self.refine = ConvModule(
                self.in_channels,
                self.in_channels,
                3,
                padding=1,
                conv_cfg=self.conv_cfg,
                norm_cfg=self.norm_cfg)
        elif self.refine_type == 'non_local':
            self.refine = NonLocal2D(
                self.in_channels,
                reduction=1,
                use_scale=False,
                conv_cfg=self.conv_cfg,
                norm_cfg=self.norm_cfg)

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                xavier_init(m, distribution='uniform')

    def forward(self, inputs):
        assert len(inputs) == self.num_levels

        # step 1: gather multi-level features by resize and average
        feats = []
        gather_size = inputs[self.refine_level].size()[2:]
        for i in range(self.num_levels):
            if i < self.refine_level:
                gathered = F.adaptive_max_pool2d(
                    inputs[i], output_size=gather_size)
            else:
                gathered = F.interpolate(
                    inputs[i], size=gather_size, mode='nearest')
            feats.append(gathered)

        if not self.output_single_lvl:
            # step 2: refine gathered features
            bsf = sum(feats) / len(feats)
            if self.refine_type is not None:
                bsf = self.refine(bsf)
            # step 3: scatter refined features to multi-levels by a residual path
            outs = []
            for i in range(self.num_levels):
                out_size = inputs[i].size()[2:]
                if i < self.refine_level:
                    residual = F.interpolate(bsf, size=out_size, mode='nearest')
                else:
                    residual = F.adaptive_max_pool2d(bsf, output_size=out_size)
                outs.append(residual + inputs[i])
            return tuple(outs)
        else:
            feats = torch.cat(feats, dim=1)
            if self.refine_type is not None:
                feats = [self.refine(feats)]
            return [feats]


@NECKS.register_module
class FRCNBFP(nn.Module):
    def __init__(self,
                 in_channels,
                 num_levels,
                 refine_level=2,
                 refine_type=None,
                 output_single_lvl=False,
                 conv_cfg=None,
                 norm_cfg=None,
                 out_channels=None,
                 c_mid=64,
                 k=15,
                 channel_expansion=1):
        super(FRCNBFP, self).__init__()
        assert refine_type in [None, 'conv', 'non_local']
        self.c_mid=c_mid
        self.k=k
        self.in_channels = in_channels
        self.num_levels = num_levels
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.output_single_lvl = output_single_lvl
        self.channel_expansion = channel_expansion

        self.refine_level = refine_level
        self.refine_type = refine_type
        self.out_channels = out_channels
        assert 0 <= self.refine_level < self.num_levels
        self.ContextModuleList = nn.ModuleList(
            [nn.Sequential(nn.Conv2d(self.in_channels[i], self.c_mid, kernel_size=(self.k, 1), padding=(int((self.k-1)/2),0)).cuda(),
                           nn.Conv2d(self.c_mid, (i+1)*49*self.channel_expansion, kernel_size=(1, self.k), padding=(0, int((self.k - 1) / 2))).cuda(),
                           )
             for i in range(len(self.in_channels))])
        #self.ConvModuleList = nn.ModuleList([nn.Conv2d(self.c_mid, (i+1)*49*self.channel_expansion, 1).cuda() for i in range(len(self.in_channels))])
        #for m in self.ConvModuleList:
        #    if isinstance(m, nn.Conv2d):
        #        xavier_init(m, distribution='uniform')

        for M in self.ContextModuleList:
            for m in M.children():
               if isinstance(m, nn.Conv2d):
                   xavier_init(m, distribution='uniform')

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                xavier_init(m, distribution='uniform')

    def forward(self, inputs):
        assert len(inputs) == self.num_levels

        # step 1: gather multi-level features by resize and average
        feats = []
        gather_size = inputs[self.refine_level].size()[2:]
        for i in range(self.num_levels):
            if i < self.refine_level:
                gathered = F.adaptive_max_pool2d(
                    inputs[i], output_size=gather_size)
            else:
                gathered = F.interpolate(
                    inputs[i], size=gather_size, mode='nearest')
            feats.append(gathered)

        for i in range(len(feats)):
            feats[i] = self.ContextModuleList[i](feats[i])
            #feats[i] = self.ConvModuleList[i](feats[i])
        feats = torch.cat(feats, dim=1)
        return [feats]
