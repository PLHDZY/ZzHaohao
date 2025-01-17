import torch.nn as nn
import torch
from swin.swin_tf import SwinStage, PatchEmbed, PatchMerging
from torchsummary import summary
from HorNet_Conv import HorBlock
from SPCAmodel import CPCAChannelAttention

def autopad(k, p=None): #kernel, padding
    # Pad to 'same'
    if p is None:
        p = k // 2 if isinstance(k, int) else [x // 2 for x in k] # auto-pad
    return p

class Conv(nn.Module):
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):
        super().__init__()
        self.conv = nn.Conv2d(c1, c2, k, s, autopad(k, p), groups=g, bias=False)
        self.bn = nn.BatchNorm2d(c2)
        self.act = nn.SiLU() if act is True else (act if isinstance(act, nn.Module) else nn.Identity())

    def forward(self, x):
        return self.act(self.bn(self.conv(x)))

    def forward_fuse(self, x):
        return self.act(self.conv(x))

class Bottleneck(nn.Module):
    def __init__(self, c1, c2, shortcut=True, g=1, e=0.5):
        super().__init__()
        c_ = int(c2 * e)
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c_, c2, 3, 1, g=g)
        self.add = shortcut and c1 == c2

    def forward(self, x):
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))

class C3(nn.Module):
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5):
        super(C3, self).__init__()
        c_ = int(c2*e)
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c1, c_, 1, 1)
        self.cv3 = Conv(2 * c_, c2, 1)
        self.m = nn.Sequential(*(Bottleneck(c_,c_, shortcut, g, e=1.0) for _ in range(n)))

    def forward(self, x):
        return self.cv3(torch.cat((self.m(self.cv1(x)), self.cv2(x)), dim=1))

class ChannelAttention(nn.Module):
    def __init__(self, in_planes, ratio=8):
        """
        第一层全连接层神经元个数较少，因此需要一个比例系数ratio进行缩放
        """
        super(ChannelAttention, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)

        """
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // ratio, False),
            nn.ReLU(),
            nn.Linear(channel // ratio, channel, False)
        )
        """
        # 利用1x1卷积代替全连接
        self.fc1 = nn.Conv2d(in_planes, in_planes // ratio, 1, bias=False)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Conv2d(in_planes // ratio, in_planes, 1, bias=False)

        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        avg_out = self.fc2(self.relu1(self.fc1(self.avg_pool(x))))
        max_out = self.fc2(self.relu1(self.fc1(self.max_pool(x))))
        out = avg_out + max_out
        return self.sigmoid(out)


class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()

        assert kernel_size in (3, 7), 'kernel size must be 3 or 7'
        padding = 3 if kernel_size == 7 else 1
        self.conv1 = nn.Conv2d(2, 1, kernel_size, padding=padding, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # x = self.SPCA(x)
        avg_out = torch.mean(x, dim=1, keepdim=True)
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        x = torch.cat([avg_out, max_out], dim=1)
        x = self.conv1(x)
        return self.sigmoid(x)

class cbam_block(nn.Module):
    def __init__(self, channel, ratio=8, kernel_size=7):
        super(cbam_block, self).__init__()
        self.channelattention = ChannelAttention(channel, ratio=ratio)
        self.spatialattention = SpatialAttention(kernel_size=kernel_size)
        self.cpcaattention = CPCAChannelAttention(channel)

    def forward(self, x):
        x = x * self.channelattention(x)
        x = x * self.spatialattention(x)
        x = x * self.cpcaattention(x)
        return x


class STHCSNet(nn.Module):
    def __init__(self):
        super(STHCSNet, self).__init__()
        self.c1 = Conv(3, 64, 3, 1, 1)
        self.c2 = PatchEmbed(64, 96, 4)
        self.cbam1 = cbam_block(96)
        self.c3 = SwinStage(96, 96, 2, 3, 7)
        self.c4 = PatchMerging(96, 192)
        self.cbam2 = cbam_block(192)
        self.c5 = SwinStage(192, 192, 2, 6, 7)
        self.c6 = PatchMerging(192, 384)
        self.c7 = SwinStage(384, 384, 6, 12, 7)
        self.c8 = PatchMerging(384, 768)
        self.c9 = SwinStage(768, 768, 2, 24, 7)
        self.conv1 = C3(768, 1024)
        self.cbam3 = cbam_block(1024)
        self.conv2 = HorBlock(1024)
        self.conv3 = C3(1024, 1536)
        self.cbam4 = cbam_block(1536)
        self.conv4 = HorBlock(1536)
        self.conv5 = C3(1536, 1024)
        self.conv6 = HorBlock(1024)
        self.conv7 = C3(1024, 768)
        self.conv8 = HorBlock(768)
        self.c11 = nn.Sequential(
            nn.Linear(7*7*768, 512),
            nn.ReLU(),
            nn.Linear(512, 512),
            nn.ReLU(),
            nn.Linear(512, 4)
        )


    def forward(self, x):
        x = self.c1(x)
        x = self.c2(x)
        x = self.cbam1(x)
        x = self.c3(x)
        x = self.c4(x)
        x = self.cbam2(x)
        x = self.c5(x)
        x = self.c6(x)
        x = self.c7(x)
        x = self.c8(x)
        x = self.c9(x)
        x = self.conv1(x)
        x = self.cbam3(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.cbam4(x)
        x = self.conv4(x)
        x = self.conv5(x)
        x = self.conv6(x)
        x = self.conv7(x)
        x = self.conv8(x)
        x = x.view(x.size(0), -1)
        x = self.c11(x)
        return x



if __name__ == "__main__":
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(device)
    vgg_model = STHCSNet().to(device)
    summary(vgg_model, (3, 224, 224))