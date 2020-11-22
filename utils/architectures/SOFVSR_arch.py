import torch
import torch.nn as nn
import torch.nn.functional as F
from utils.architectures.video import optical_flow_warp


#TODO: 
# - change pixelshuffle upscales with available options in block (can also add pa_unconv with pixel attention)
# - make the upscaling layers automatic
# - add the network configuration parameters to the init to pass from options file

class SOFVSR(nn.Module):
    def __init__(self, scale=4, n_frames=3, channels=320):
        super(SOFVSR, self).__init__()
        self.scale = scale
        self.OFR = OFRnet(scale=scale, channels=channels)
        self.SR = SRnet(scale=scale, channels=channels, n_frames=n_frames)

    def forward(self, x):
        # x: b*n*c*h*w
        b, n_frames, c, h, w = x.size()
        idx_center = (n_frames - 1) // 2

        # motion estimation
        flow_L1 = []
        flow_L2 = []
        flow_L3 = []
        input = []

        for idx_frame in range(n_frames):
            if idx_frame != idx_center:
                input.append(torch.cat((x[:,idx_frame,:,:,:], x[:,idx_center,:,:,:]), 1))
        optical_flow_L1, optical_flow_L2, optical_flow_L3 = self.OFR(torch.cat(input, 0))

        optical_flow_L1 = optical_flow_L1.view(-1, b, 2, h//2, w//2)
        optical_flow_L2 = optical_flow_L2.view(-1, b, 2, h, w)
        optical_flow_L3 = optical_flow_L3.view(-1, b, 2, h*self.scale, w*self.scale)

        # motion compensation
        draft_cube = []
        draft_cube.append(x[:, idx_center, :, :, :])

        for idx_frame in range(n_frames):
            if idx_frame == idx_center:
                flow_L1.append([])
                flow_L2.append([])
                flow_L3.append([])
            else: # if idx_frame != idx_center:
                if idx_frame < idx_center:
                    idx = idx_frame
                if idx_frame > idx_center:
                    idx = idx_frame - 1

                flow_L1.append(optical_flow_L1[idx, :, :, :, :])
                flow_L2.append(optical_flow_L2[idx, :, :, :, :])
                flow_L3.append(optical_flow_L3[idx, :, :, :, :])

                # Generate the draft_cube by subsampling the SR flow optical_flow_L3
                # according to the scale
                for i in range(self.scale):
                    for j in range(self.scale):
                        draft = optical_flow_warp(x[:, idx_frame, :, :, :],
                                                  optical_flow_L3[idx, :, :, i::self.scale, j::self.scale] / self.scale)
                        draft_cube.append(draft)
        draft_cube = torch.cat(draft_cube, 1)

        # super-resolution
        SR = self.SR(draft_cube)

        return flow_L1, flow_L2, flow_L3, SR


class OFRnet(nn.Module):
    def __init__(self, scale, channels):
        super(OFRnet, self).__init__()
        self.pool = nn.AvgPool2d(2)
        self.scale = scale

        ## RNN part
        self.RNN1 = nn.Sequential(
            nn.Conv2d(4, channels, 3, 1, 1, bias=False), # TODO: change 4 to 8 for 3 channel images
            nn.LeakyReLU(0.1, inplace=True),
            CasResB(3, channels)
        )
        self.RNN2 = nn.Sequential(
            nn.Conv2d(channels, 2, 3, 1, 1, bias=False), # TODO: change 2 to 6 for 3 channel images
        )

        # SR part
        SR = []
        SR.append(CasResB(3, channels))
        if self.scale == 4:
            SR.append(nn.Conv2d(channels, 64 * 4, 1, 1, 0, bias=False))
            SR.append(nn.PixelShuffle(2)) #TODO
            SR.append(nn.LeakyReLU(0.1, inplace=True))
            SR.append(nn.Conv2d(64, 64 * 4, 1, 1, 0, bias=False))
            SR.append(nn.PixelShuffle(2)) #TODO
            SR.append(nn.LeakyReLU(0.1, inplace=True))
        elif self.scale == 3:
            SR.append(nn.Conv2d(channels, 64 * 9, 1, 1, 0, bias=False))
            SR.append(nn.PixelShuffle(3)) #TODO
            SR.append(nn.LeakyReLU(0.1, inplace=True))
        elif self.scale == 2:
            SR.append(nn.Conv2d(channels, 64 * 4, 1, 1, 0, bias=False))
            SR.append(nn.PixelShuffle(2)) #TODO
            SR.append(nn.LeakyReLU(0.1, inplace=True))
        #TODO: test scale 1x
        elif self.scale == 1:
            SR.append(nn.Conv2d(channels, 64 * 1, 1, 1, 0, bias=False))
            SR.append(nn.LeakyReLU(0.1, inplace=True))
        SR.append(nn.Conv2d(64, 2, 3, 1, 1, bias=False))

        self.SR = nn.Sequential(*SR)

    def __call__(self, x):
        # x: b*2*h*w
        #Part 1
        x_L1 = self.pool(x)
        b, c, h, w = x_L1.size()
        input_L1 = torch.cat((x_L1, torch.zeros(b, 2, h, w).cuda()), 1)
        optical_flow_L1 = self.RNN2(self.RNN1(input_L1))
        # optical_flow_L1_upscaled = F.interpolate(optical_flow_L1, scale_factor=2, mode='bilinear', align_corners=False) * 2
        
        # TODO: check, temporary fix, since the original interpolation was not producing the correct shape required in Part 2
        # in optical_flow_warp, instead of shape torch.Size([2, 1, 66, 75]) like the image, it was producing torch.Size([2, 1, 66, 74])
        # here I'm forcing it to be interpolated to exactly the size of the image
        image_shape = torch.unsqueeze(x[:, 0, :, :], 1).shape
        optical_flow_L1_upscaled = F.interpolate(optical_flow_L1, size=(image_shape[2],image_shape[3]), mode='bilinear', align_corners=False) * 2
        # print(optical_flow_L1_upscaled.shape)
        # print(torch.unsqueeze(x[:, 0, :, :], 1).shape)

        #Part 2
        x_L2 = optical_flow_warp(torch.unsqueeze(x[:, 0, :, :], 1), optical_flow_L1_upscaled)
        input_L2 = torch.cat((x_L2, torch.unsqueeze(x[:, 1, :, :], 1), optical_flow_L1_upscaled), 1)
        optical_flow_L2 = self.RNN2(self.RNN1(input_L2)) + optical_flow_L1_upscaled

        #Part 3
        x_L3 = optical_flow_warp(torch.unsqueeze(x[:, 0, :, :], 1), optical_flow_L2)
        input_L3 = torch.cat((x_L3, torch.unsqueeze(x[:, 1, :, :], 1), optical_flow_L2), 1)
        #TODO: 3 channel images breaks here, because the first part has only 2 channels (2 * 1) and the second part now has 6 channels (2 * 3)
        optical_flow_L3 = self.SR(self.RNN1(input_L3)) + \
                          F.interpolate(optical_flow_L2, scale_factor=self.scale, mode='bilinear', align_corners=False) * self.scale
        return optical_flow_L1, optical_flow_L2, optical_flow_L3


class SRnet(nn.Module):
    def __init__(self, scale, channels, n_frames):
        super(SRnet, self).__init__()
        body = []
        # scale ** 2 -> due to the subsampling of the SR flow 
        body.append(nn.Conv2d(1 * scale ** 2 * (n_frames-1) + 1, channels, 3, 1, 1, bias=False))
        body.append(nn.LeakyReLU(0.1, inplace=True))
        body.append(CasResB(8, channels))
        if scale == 4:
            body.append(nn.Conv2d(channels, 64 * 4, 1, 1, 0, bias=False))
            body.append(nn.PixelShuffle(2)) #TODO
            body.append(nn.LeakyReLU(0.1, inplace=True))
            body.append(nn.Conv2d(64, 64 * 4, 1, 1, 0, bias=False))
            body.append(nn.PixelShuffle(2)) #TODO
            body.append(nn.LeakyReLU(0.1, inplace=True))
        elif scale == 3:
            body.append(nn.Conv2d(channels, 64 * 9, 1, 1, 0, bias=False))
            body.append(nn.PixelShuffle(3)) #TODO
            body.append(nn.LeakyReLU(0.1, inplace=True))
        elif scale == 2:
            body.append(nn.Conv2d(channels, 64 * 4, 1, 1, 0, bias=False))
            body.append(nn.PixelShuffle(2)) #TODO
            body.append(nn.LeakyReLU(0.1, inplace=True))
        #TODO: test scale 1x
        elif scale == 1:
            body.append(nn.Conv2d(channels, 64 * 1, 1, 1, 0, bias=False))
            body.append(nn.LeakyReLU(0.1, inplace=True))
        body.append(nn.Conv2d(64, 1, 3, 1, 1, bias=True))

        self.body = nn.Sequential(*body)

    def __call__(self, x):
        out = self.body(x)
        return out


class ResB(nn.Module):
    def __init__(self, channels):
        super(ResB, self).__init__()
        self.body = nn.Sequential(
            nn.Conv2d(channels//2, channels//2, 1, 1, 0, bias=False),
            nn.LeakyReLU(0.1, inplace=True),
            nn.Conv2d(channels//2, channels//2, 3, 1, 1, bias=False, groups=channels//2),
            nn.Conv2d(channels // 2, channels // 2, 1, 1, 0, bias=False),
            nn.LeakyReLU(0.1, inplace=True),
        )
    def forward(self, x):
        input = x[:, x.shape[1]//2:, :, :]
        out = torch.cat((x[:, :x.shape[1]//2, :, :], self.body(input)), 1)
        return channel_shuffle(out, 2)


class CasResB(nn.Module):
    def __init__(self, n_ResB, channels):
        super(CasResB, self).__init__()
        body = []
        for i in range(n_ResB):
            body.append(ResB(channels))
        self.body = nn.Sequential(*body)
    def forward(self, x):
        return self.body(x)


def channel_shuffle(x, groups):
    b, c, h, w = x.size()
    x = x.view(b, groups, c//groups,  h, w)
    x = x.permute(0, 2, 1, 3, 4).contiguous()
    x = x.view(b, -1, h, w)
    return x
