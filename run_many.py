#!/usr/bin/env python
"""Compute the optical flow for a list of images without reloading the network everytime
(but still uses 1 pair at a time)

Use: python run_many --model=default pair_file.txt

pair_file.txt contains the list of images the network should be run on with the output name:
img1.ppm img2.ppm flow1.flo
img2.ppm img3.ppm flow2.flo
...
"""
import torch

import getopt
import math
import numpy
import os
import PIL
import PIL.Image
import sys

from argparse import ArgumentParser

try:
        from .correlation import correlation # the custom cost volume layer
except:
	sys.path.insert(0, './correlation'); import correlation # you should consider upgrading python
        # end

##########################################################

torch.set_grad_enabled(False) # make sure to not compute gradients for computational performance
torch.backends.cudnn.enabled = True # make sure to use cudnn for computational performance

##########################################################


parser = ArgumentParser("Compute optical flow between multiple pairs of images")
parser.add_argument("--model", default="default")
parser.add_argument("pair_list", type=str)
args = parser.parse_args()

##########################################################

backwarp_tenGrid = {}

def backwarp(tenInput, tenFlow):
	if str(tenFlow.size()) not in backwarp_tenGrid:
		tenHorizontal = torch.linspace(-1.0, 1.0, tenFlow.shape[3]).view(1, 1, 1, tenFlow.shape[3]).expand(tenFlow.shape[0], -1, tenFlow.shape[2], -1)
		tenVertical = torch.linspace(-1.0, 1.0, tenFlow.shape[2]).view(1, 1, tenFlow.shape[2], 1).expand(tenFlow.shape[0], -1, -1, tenFlow.shape[3])

		backwarp_tenGrid[str(tenFlow.size())] = torch.cat([ tenHorizontal, tenVertical ], 1).cuda()
	        # end

	tenFlow = torch.cat([ tenFlow[:, 0:1, :, :] / ((tenInput.shape[3] - 1.0) / 2.0), tenFlow[:, 1:2, :, :] / ((tenInput.shape[2] - 1.0) / 2.0) ], 1)

	return torch.nn.functional.grid_sample(input=tenInput, grid=(backwarp_tenGrid[str(tenFlow.size())] + tenFlow).permute(0, 2, 3, 1), mode='bilinear', padding_mode='zeros')
# end

##########################################################

class Network(torch.nn.Module):
	def __init__(self):
		super(Network, self).__init__()

		class Features(torch.nn.Module):
			def __init__(self):
				super(Features, self).__init__()

				self.netOne = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=3, out_channels=32, kernel_size=7, stride=1, padding=3),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)

				self.netTwo = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=2, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)

				self.netThr = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3, stride=2, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)

				self.netFou = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=64, out_channels=96, kernel_size=3, stride=2, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=96, out_channels=96, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)

				self.netFiv = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=96, out_channels=128, kernel_size=3, stride=2, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)

				self.netSix = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=128, out_channels=192, kernel_size=3, stride=2, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)
			        # end

			def forward(self, tenInput):
				tenOne = self.netOne(tenInput)
				tenTwo = self.netTwo(tenOne)
				tenThr = self.netThr(tenTwo)
				tenFou = self.netFou(tenThr)
				tenFiv = self.netFiv(tenFou)
				tenSix = self.netSix(tenFiv)

				return [ tenOne, tenTwo, tenThr, tenFou, tenFiv, tenSix ]
			# end
		# end

		class Matching(torch.nn.Module):
			def __init__(self, intLevel):
				super(Matching, self).__init__()

				self.fltBackwarp = [ 0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625 ][intLevel]

				if intLevel != 2:
					self.netFeat = torch.nn.Sequential()

				elif intLevel == 2:
					self.netFeat = torch.nn.Sequential(
						torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=1, stride=1, padding=0),
						torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
					)

				# end

				if intLevel == 6:
					self.netUpflow = None

				elif intLevel != 6:
					self.netUpflow = torch.nn.ConvTranspose2d(in_channels=2, out_channels=2, kernel_size=4, stride=2, padding=1, bias=False, groups=2)

				# end

				if intLevel >= 4:
					self.netUpcorr = None

				elif intLevel < 4:
					self.netUpcorr = torch.nn.ConvTranspose2d(in_channels=49, out_channels=49, kernel_size=4, stride=2, padding=1, bias=False, groups=49)

				# end

				self.netMain = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=49, out_channels=128, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=32, out_channels=2, kernel_size=[ 0, 0, 7, 5, 5, 3, 3 ][intLevel], stride=1, padding=[ 0, 0, 3, 2, 2, 1, 1 ][intLevel])
				)
			        # end

			def forward(self, tenFirst, tenSecond, tenFeaturesFirst, tenFeaturesSecond, tenFlow):
				tenFeaturesFirst = self.netFeat(tenFeaturesFirst)
				tenFeaturesSecond = self.netFeat(tenFeaturesSecond)

				if tenFlow is not None:
					tenFlow = self.netUpflow(tenFlow)
				        # end

				if tenFlow is not None:
					tenFeaturesSecond = backwarp(tenInput=tenFeaturesSecond, tenFlow=tenFlow * self.fltBackwarp)
				        # end

				if self.netUpcorr is None:
					tenCorrelation = torch.nn.functional.leaky_relu(input=correlation.FunctionCorrelation(tenFirst=tenFeaturesFirst, tenSecond=tenFeaturesSecond, intStride=1), negative_slope=0.1, inplace=False)

				elif self.netUpcorr is not None:
					tenCorrelation = self.netUpcorr(torch.nn.functional.leaky_relu(input=correlation.FunctionCorrelation(tenFirst=tenFeaturesFirst, tenSecond=tenFeaturesSecond, intStride=2), negative_slope=0.1, inplace=False))

				# end

				return (tenFlow if tenFlow is not None else 0.0) + self.netMain(tenCorrelation)
			# end
		# end

		class Subpixel(torch.nn.Module):
			def __init__(self, intLevel):
				super(Subpixel, self).__init__()

				self.fltBackward = [ 0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625 ][intLevel]

				if intLevel != 2:
					self.netFeat = torch.nn.Sequential()

				elif intLevel == 2:
					self.netFeat = torch.nn.Sequential(
						torch.nn.Conv2d(in_channels=32, out_channels=64, kernel_size=1, stride=1, padding=0),
						torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
					)

				# end

				self.netMain = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=[ 0, 0, 130, 130, 194, 258, 386 ][intLevel], out_channels=128, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=32, out_channels=2, kernel_size=[ 0, 0, 7, 5, 5, 3, 3 ][intLevel], stride=1, padding=[ 0, 0, 3, 2, 2, 1, 1 ][intLevel])
				)
			        # end

			def forward(self, tenFirst, tenSecond, tenFeaturesFirst, tenFeaturesSecond, tenFlow):
				tenFeaturesFirst = self.netFeat(tenFeaturesFirst)
				tenFeaturesSecond = self.netFeat(tenFeaturesSecond)

				if tenFlow is not None:
					tenFeaturesSecond = backwarp(tenInput=tenFeaturesSecond, tenFlow=tenFlow * self.fltBackward)
				        # end

				return (tenFlow if tenFlow is not None else 0.0) + self.netMain(torch.cat([ tenFeaturesFirst, tenFeaturesSecond, tenFlow ], 1))
			# end
		# end

		class Regularization(torch.nn.Module):
			def __init__(self, intLevel):
				super(Regularization, self).__init__()

				self.fltBackward = [ 0.0, 0.0, 10.0, 5.0, 2.5, 1.25, 0.625 ][intLevel]

				self.intUnfold = [ 0, 0, 7, 5, 5, 3, 3 ][intLevel]

				if intLevel >= 5:
					self.netFeat = torch.nn.Sequential()

				elif intLevel < 5:
					self.netFeat = torch.nn.Sequential(
						torch.nn.Conv2d(in_channels=[ 0, 0, 32, 64, 96, 128, 192 ][intLevel], out_channels=128, kernel_size=1, stride=1, padding=0),
						torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
					)

				# end

				self.netMain = torch.nn.Sequential(
					torch.nn.Conv2d(in_channels=[ 0, 0, 131, 131, 131, 131, 195 ][intLevel], out_channels=128, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=64, out_channels=64, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
					torch.nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, stride=1, padding=1),
					torch.nn.LeakyReLU(inplace=False, negative_slope=0.1)
				)

				if intLevel >= 5:
					self.netDist = torch.nn.Sequential(
						torch.nn.Conv2d(in_channels=32, out_channels=[ 0, 0, 49, 25, 25, 9, 9 ][intLevel], kernel_size=[ 0, 0, 7, 5, 5, 3, 3 ][intLevel], stride=1, padding=[ 0, 0, 3, 2, 2, 1, 1 ][intLevel])
					)

				elif intLevel < 5:
					self.netDist = torch.nn.Sequential(
						torch.nn.Conv2d(in_channels=32, out_channels=[ 0, 0, 49, 25, 25, 9, 9 ][intLevel], kernel_size=([ 0, 0, 7, 5, 5, 3, 3 ][intLevel], 1), stride=1, padding=([ 0, 0, 3, 2, 2, 1, 1 ][intLevel], 0)),
						torch.nn.Conv2d(in_channels=[ 0, 0, 49, 25, 25, 9, 9 ][intLevel], out_channels=[ 0, 0, 49, 25, 25, 9, 9 ][intLevel], kernel_size=(1, [ 0, 0, 7, 5, 5, 3, 3 ][intLevel]), stride=1, padding=(0, [ 0, 0, 3, 2, 2, 1, 1 ][intLevel]))
					)

				# end

				self.netScaleX = torch.nn.Conv2d(in_channels=[ 0, 0, 49, 25, 25, 9, 9 ][intLevel], out_channels=1, kernel_size=1, stride=1, padding=0)
				self.netScaleY = torch.nn.Conv2d(in_channels=[ 0, 0, 49, 25, 25, 9, 9 ][intLevel], out_channels=1, kernel_size=1, stride=1, padding=0)
			        # eny

			def forward(self, tenFirst, tenSecond, tenFeaturesFirst, tenFeaturesSecond, tenFlow):
				tenDifference = (tenFirst - backwarp(tenInput=tenSecond, tenFlow=tenFlow * self.fltBackward)).pow(2.0).sum(1, True).sqrt().detach()

				tenDist = self.netDist(self.netMain(torch.cat([ tenDifference, tenFlow - tenFlow.view(tenFlow.shape[0], 2, -1).mean(2, True).view(tenFlow.shape[0], 2, 1, 1), self.netFeat(tenFeaturesFirst) ], 1)))
				tenDist = tenDist.pow(2.0).neg()
				tenDist = (tenDist - tenDist.max(1, True)[0]).exp()

				tenDivisor = tenDist.sum(1, True).reciprocal()

				tenScaleX = self.netScaleX(tenDist * torch.nn.functional.unfold(input=tenFlow[:, 0:1, :, :], kernel_size=self.intUnfold, stride=1, padding=int((self.intUnfold - 1) / 2)).view_as(tenDist)) * tenDivisor
				tenScaleY = self.netScaleY(tenDist * torch.nn.functional.unfold(input=tenFlow[:, 1:2, :, :], kernel_size=self.intUnfold, stride=1, padding=int((self.intUnfold - 1) / 2)).view_as(tenDist)) * tenDivisor

				return torch.cat([ tenScaleX, tenScaleY ], 1)
			# end
		# end

		self.netFeatures = Features()
		self.netMatching = torch.nn.ModuleList([ Matching(intLevel) for intLevel in [ 2, 3, 4, 5, 6 ] ])
		self.netSubpixel = torch.nn.ModuleList([ Subpixel(intLevel) for intLevel in [ 2, 3, 4, 5, 6 ] ])
		self.netRegularization = torch.nn.ModuleList([ Regularization(intLevel) for intLevel in [ 2, 3, 4, 5, 6 ] ])

		self.load_state_dict({ strKey.replace('module', 'net'): tenWeight for strKey, tenWeight in torch.load(__file__.replace('run_many.py', 'network-' + args.model + '.pytorch')).items() })
	        # end

	def forward(self, tenFirst, tenSecond):
		tenFirst[:, 0, :, :] = tenFirst[:, 0, :, :] - 0.411618
		tenFirst[:, 1, :, :] = tenFirst[:, 1, :, :] - 0.434631
		tenFirst[:, 2, :, :] = tenFirst[:, 2, :, :] - 0.454253

		tenSecond[:, 0, :, :] = tenSecond[:, 0, :, :] - 0.410782
		tenSecond[:, 1, :, :] = tenSecond[:, 1, :, :] - 0.433645
		tenSecond[:, 2, :, :] = tenSecond[:, 2, :, :] - 0.452793

		tenFeaturesFirst = self.netFeatures(tenFirst)
		tenFeaturesSecond = self.netFeatures(tenSecond)

		tenFirst = [ tenFirst ]
		tenSecond = [ tenSecond ]

		for intLevel in [ 1, 2, 3, 4, 5 ]:
			tenFirst.append(torch.nn.functional.interpolate(input=tenFirst[-1], size=(tenFeaturesFirst[intLevel].shape[2], tenFeaturesFirst[intLevel].shape[3]), mode='bilinear', align_corners=False))
			tenSecond.append(torch.nn.functional.interpolate(input=tenSecond[-1], size=(tenFeaturesSecond[intLevel].shape[2], tenFeaturesSecond[intLevel].shape[3]), mode='bilinear', align_corners=False))
		        # end

		tenFlow = None

		for intLevel in [ -1, -2, -3, -4, -5 ]:
			tenFlow = self.netMatching[intLevel](tenFirst[intLevel], tenSecond[intLevel], tenFeaturesFirst[intLevel], tenFeaturesSecond[intLevel], tenFlow)
			tenFlow = self.netSubpixel[intLevel](tenFirst[intLevel], tenSecond[intLevel], tenFeaturesFirst[intLevel], tenFeaturesSecond[intLevel], tenFlow)
			tenFlow = self.netRegularization[intLevel](tenFirst[intLevel], tenSecond[intLevel], tenFeaturesFirst[intLevel], tenFeaturesSecond[intLevel], tenFlow)
		        # end

		return tenFlow * 20.0
	# end
# end

netNetwork = None

##########################################################

def estimate(tenFirst, tenSecond):
	global netNetwork

	if netNetwork is None:
		netNetwork = Network().cuda().eval()
	        # end

	assert(tenFirst.shape[1] == tenSecond.shape[1])
	assert(tenFirst.shape[2] == tenSecond.shape[2])

	intWidth = tenFirst.shape[2]
	intHeight = tenFirst.shape[1]

	#assert(intWidth == 1024) # remember that there is no guarantee for correctness, comment this line out if you acknowledge this and want to continue
	#assert(intHeight == 436) # remember that there is no guarantee for correctness, comment this line out if you acknowledge this and want to continue

	tenPreprocessedFirst = tenFirst.cuda().view(1, 3, intHeight, intWidth)
	tenPreprocessedSecond = tenSecond.cuda().view(1, 3, intHeight, intWidth)

	intPreprocessedWidth = int(math.floor(math.ceil(intWidth / 32.0) * 32.0))
	intPreprocessedHeight = int(math.floor(math.ceil(intHeight / 32.0) * 32.0))

	tenPreprocessedFirst = torch.nn.functional.interpolate(input=tenPreprocessedFirst, size=(intPreprocessedHeight, intPreprocessedWidth), mode='bilinear', align_corners=False)
	tenPreprocessedSecond = torch.nn.functional.interpolate(input=tenPreprocessedSecond, size=(intPreprocessedHeight, intPreprocessedWidth), mode='bilinear', align_corners=False)

	tenFlow = torch.nn.functional.interpolate(input=netNetwork(tenPreprocessedFirst, tenPreprocessedSecond), size=(intHeight, intWidth), mode='bilinear', align_corners=False)

	tenFlow[:, 0, :, :] *= float(intWidth) / float(intPreprocessedWidth)
	tenFlow[:, 1, :, :] *= float(intHeight) / float(intPreprocessedHeight)

	return tenFlow[0, :, :, :].cpu()
# end

##########################################################

if __name__ == '__main__':
        # Read the list of images
        with open(args.pair_list) as f:
                pairs = [x.strip().split(" ") for x in f.readlines()]

        for left, right, output in pairs:
	        tenFirst = torch.FloatTensor(numpy.array(PIL.Image.open(left))[:, :, ::-1].transpose(2, 0, 1).astype(numpy.float32) * (1.0 / 255.0))
	        tenSecond = torch.FloatTensor(numpy.array(PIL.Image.open(right))[:, :, ::-1].transpose(2, 0, 1).astype(numpy.float32) * (1.0 / 255.0))

	        tenOutput = estimate(tenFirst, tenSecond)

	        objOutput = open(output, 'wb')
	        numpy.array([ 80, 73, 69, 72 ], numpy.uint8).tofile(objOutput)
	        numpy.array([ tenOutput.shape[2], tenOutput.shape[1] ], numpy.int32).tofile(objOutput)
	        numpy.array(tenOutput.numpy().transpose(1, 2, 0), numpy.float32).tofile(objOutput)
	        objOutput.close()
                # end
