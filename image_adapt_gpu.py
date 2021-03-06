import numpy as np
from pylab import *
import os
import sys
import pycuda.autoinit
import pycuda.driver as cu
import pycuda.compiler as nvcc
from pycuda import gpuarray
from pycuda.reduction import ReductionKernel
from pycuda.elementwise import ElementwiseKernel
import time

# Set debug to 1 to output debug.txt
DEBUG = 0

"""
# DOES NOT SEEM TO WORK FOR RESONANCE
#Get the input filename from the command line
try:
    file_name = sys.argv[1]; MaxRad = float(sys.argv[2]); Threshold = float(sys.argv[3])
except:
    print "Usage:",sys.argv[0], "infile maxrad threshold"; sys.exit(1)
"""

file_name   = 'extrap_data/11759_ccd3/11759_512x512.png'
Threshold   = np.int32(15)
MaxRad      = np.int32(4)


# setup input file
IMG_rgb = imread(file_name)
IMG = array( IMG_rgb[:,:,0] )

# Get image data
Lx = np.int32( IMG.shape[0] )
Ly = np.int32( IMG.shape[1] )

total_start_time = time.time()
setup_start_time = time.time()

# Allocate memory
# size of the box needed to reach the threshold value or maxrad value
BOX = np.zeros((Lx, Ly), dtype=np.float32)
# normalized array
NORM = np.zeros((Lx, Ly), dtype=np.float32)
# output array
OUT = np.zeros((Lx, Ly), dtype=np.float32)

# Execution configuration
TPBx	= int( 32 )                # Sweet spot num of threads per block
TPBy    = int( 32 )                # 32*32 = 1024 max
nBx     = int( Ly/TPBx )           # Num of thread blocks
nBy     = int( Lx/TPBy ) 


#kernel for first part of algorithm that performs the smoothing
## TO DO ##   
# Implement The kernel
#########
kernel_smooth_source = \
"""
    __global__ void smoothingFilter(int Lx, int Ly, int Threshold, int MaxRad, 
        float* IMG, float* BOX, float* NORM)
    {
    // Indexing
    int tid = threadIdx.x;
    int tjd = threadIdx.y;
    int i = blockIdx.x * blockDim.x + tid;
    int j = blockIdx.y * blockDim.y + tjd;
    int stid = tjd * blockDim.x + tid;
    int gtid = j * Ly + i;  
    
    // Smoothing params
    float qq    = 1.0;
    float sum   = 0.0;
    float ksum  = 0.0;
    float ss    = qq;

    // Shared memory
    extern __shared__ float s_IMG[];
    s_IMG[stid] = IMG[gtid];
    __syncthreads();
    
    // Compute all pixels except for image border
	if ( i >= 0 && i < Ly && j >= 0 && j < Lx )
	{
	    // Continue until parameters are met
	    while (sum < Threshold && qq < MaxRad)
	    {
	        ss = qq;
	        sum = 0.0;
	        ksum = 0.0;
	        
            // Normal adaptive smoothing (w/o gaussian sum)
            for (int ii = -ss; ii < ss+1; ii++)
            {
                for (int jj = -ss; jj < ss+1; jj++)
                {
                    if ( (i-ss >= 0) && (i+ss < Lx) && (j-ss >= 0) && (j+ss < Ly) )
                    {
                         // Compute within bounds of block dimensions
                        if( tid-ss > 0 && tid+ss < blockDim.x && tjd-ss > 0 && tjd+ss < blockDim.y )
                        {
                            sum += s_IMG[stid + ii*blockDim.y + jj];
                            ksum += 1.0;          
                        }
                        // Compute block borders with global memory
                        else
                        {
                            sum += IMG[gtid + ii*Ly + jj];
                            ksum += 1.0;                   
                        }                                     
                    }
                }
            }
            qq += 1;
        }
        BOX[gtid] = ss;
        __syncthreads();

        // Determine the normalization for each box
        for (int ii = -ss; ii < ss+1; ii++)
        {
            for (int jj = -ss; jj < ss+1; jj++)
            {
                if (ksum != 0)
                {
                    NORM[gtid + ii*Ly + jj] +=  1.0 / ksum;
                }
            }
        }
	}
	__syncthreads();

    }
    """    
#kernel for the second part of the algorithm that normalizes the data
## TO DO ##   
# Implement The kernel
#########
kernel_norm_source = \
"""
    __global__ void normalizeFilter(int Lx, int Ly, float* IMG, float* NORM )
    {
    // Indexing
    int tid = threadIdx.x;
    int tjd = threadIdx.y;
    int i = blockIdx.x * blockDim.x + tid;
    int j = blockIdx.y * blockDim.y + tjd;
    int stid = tjd * blockDim.x + tid;
    int gtid = j * Ly + i;  

    // shared memory for IMG and NORM
    extern __shared__ float s_NORM[];
    s_NORM[stid] = NORM[gtid];
    __syncthreads();    

    // Compute all pixels except for image border
	if ( i >= 0 && i < Ly && j >= 0 && j < Lx )
	{
        // Compute within bounds of block dimensions
        if( tid > 0 && tid < blockDim.x && tjd > 0 && tjd < blockDim.y )
        {
            if (s_NORM[stid] != 0)
            {
                IMG[gtid] /= s_NORM[stid];
            }
        }
        // Compute block borders with global memory
        else
        {
            if (NORM[gtid] != 0)
            {        
                IMG[gtid] /= NORM[gtid];
            }
        }
	}
	__syncthreads();
    }
"""

#kernel for the last part of the algorithm that creates the output image
## TO DO ##   
# Implement The kernel
#########
kernel_out_source = \
"""
    __global__ void outFilter( int Lx, int Ly, float* IMG, float* BOX, float* OUT )
    {
    // Indexing
    int tid = threadIdx.x;
    int tjd = threadIdx.y;
    int i = blockIdx.x * blockDim.x + tid;
    int j = blockIdx.y * blockDim.y + tjd;
    int stid = tjd * blockDim.x + tid;
    int gtid = j * Ly + i;  

    // Smoothing params
    float ss    = BOX[gtid];
    float sum   = 0.0;
    float ksum  = 0.0;

    extern __shared__ float s_IMG[];
    s_IMG[stid] = IMG[gtid];
    __syncthreads();

    // Compute all pixels except for image border
	if ( i >= 0 && i < Ly && j >= 0 && j < Lx )
	{
        for (int ii = -ss; ii < ss+1; ii++)
        {
            for (int jj = -ss; jj < ss+1; jj++)
            {
            if ( (i-ss >= 0) && (i+ss < Lx) && (j-ss >= 0) && (j+ss < Ly) )
                {
                     // Compute within bounds of block dimensions
                    if( tid-ss > 0 && tid+ss < blockDim.x && tjd-ss > 0 && tjd+ss < blockDim.y )
                    {
                        sum += s_IMG[stid + ii*blockDim.y + jj];
                        ksum += 1.0;          
                    }
                    // Compute block borders with global memory
                    else
                    {
                        sum += IMG[gtid + ii*Ly + jj];
                        ksum += 1.0;                   
                    }
                }
            }
        }
	}  
    if ( ksum != 0 )
    {
        OUT[gtid] = sum / ksum;
    }
	__syncthreads();
    }
"""

# Initialize kernel
smoothing_kernel = nvcc.SourceModule(kernel_smooth_source).get_function("smoothingFilter")
normalize_kernel = nvcc.SourceModule(kernel_norm_source).get_function("normalizeFilter")
out_kernel = nvcc.SourceModule(kernel_out_source).get_function("outFilter")



# Allocate memory and constants
smem_size   = int(TPBx*TPBy*4)

# Copy arrays to device once
IMG_device          = gpuarray.to_gpu(IMG)
BOX_device          = gpuarray.to_gpu(BOX)
NORM_device         = gpuarray.to_gpu(NORM)
OUT_device          = gpuarray.to_gpu(OUT)

setup_stop_time = time.time()
smth_kernel_start_time   = cu.Event()
smth_kernel_stop_time    = cu.Event()
norm_kernel_start_time   = cu.Event()
norm_kernel_stop_time    = cu.Event()
out_kernel_start_time    = cu.Event()
out_kernel_stop_time     = cu.Event()

##########
# The kernel will convolve the image with a gaussian weighted sum
# determine the BOX size that allows the sum to reach either the maxRad or 
# threshold values
# This kernel will utilize the IMG and modify the BOX and NORM
##########

smth_kernel_start_time.record()
smoothing_kernel(Lx, Ly, Threshold, MaxRad, IMG_device, BOX_device, NORM_device,
    block=( TPBx, TPBy,1 ),  grid=( nBx, nBy ), shared=( smem_size ) )
smth_kernel_stop_time.record()

##########
# This kernel will normalize the image with the value obtained from first kernel
# Normalizing kernel will utilize the NORM and modify the IMG
##########

# Copy normalization factor to host and box size array
# No need because it is already stored on device
# NORM = NORM_device.get()
# BOX = BOX_device.get()

norm_kernel_start_time.record()
normalize_kernel(Lx, Ly, IMG_device, NORM_device,
    block=( TPBx, TPBy,1 ),  grid=( nBx, nBy ), shared=( smem_size ) )
norm_kernel_stop_time.record()

# Copy image to host and send to output kernel
# No need because it is already stored on device
# IMG_norm = IMG_norm_device.get()

##########
# This will resmooth the data utilizing the new normalized image
# This kernel will utilize the BOX and IMG_norm and modify the OUT
##########
out_kernel_start_time.record()
out_kernel(Lx, Ly, IMG_device, BOX_device, OUT_device,
    block=( TPBx, TPBy,1 ),  grid=( nBx, nBy ), shared=( smem_size ) )
out_kernel_stop_time.record()

# Copy image to host and print
IMG_out = OUT_device.get()
imsave('{}_smoothed_gpu_s.png'.format(os.path.splitext(file_name)[0]), IMG_out, cmap=cm.gray, vmin=0, vmax=1)
total_stop_time = time.time()

if (DEBUG):
    BOX_out = BOX_device.get()
    NORM_out = NORM_device.get()
    # Debug
    f = open('debug_gpu_s.txt', 'w')
    set_printoptions(threshold='nan')
    print >>f,'IMG'
    print >>f, str(IMG).replace('[',' ').replace(']', ' ')
    print >>f,'OUTPUT'
    print >>f, str(IMG_out).replace('[',' ').replace(']', ' ')
    print >>f,'BOX'
    print >>f, str(BOX_out).replace('[',' ').replace(']', ' ')
    print >>f,'NORM'
    print >>f, str(NORM_out).replace('[',' ').replace(']', ' ')
    f.close()

# Print results & save

setup_time      = (setup_stop_time - setup_start_time)
smth_ker_time   = (smth_kernel_start_time.time_till(smth_kernel_stop_time) * 1e-3)
norm_ker_time   = (norm_kernel_start_time.time_till(norm_kernel_stop_time) * 1e-3)
out_ker_time    = (out_kernel_start_time.time_till(out_kernel_stop_time) * 1e-3)
total_time      = setup_time + smth_ker_time + norm_ker_time + out_ker_time

print "Total Time: %f"                  % total_time
print "Setup Time: %f"                  % setup_time
print "Kernel (Smooth) Time: %f"        % smth_ker_time
print "Kernel (Normalize) Time: %f"     % norm_ker_time
print "Kernel (Output) Time: %f"        % out_ker_time

