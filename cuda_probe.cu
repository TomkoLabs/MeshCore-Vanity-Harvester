#include <cuda_runtime.h>

#include <cstdio>
#include <cstdlib>

static void check(cudaError_t result, const char *operation)
{
    if (result != cudaSuccess) {
        std::fprintf(
            stderr,
            "%s failed: %d (%s)\n",
            operation,
            static_cast<int>(result),
            cudaGetErrorString(result)
        );
        std::exit(1);
    }
}

__global__ void set_value(int *value)
{
    *value = 42;
}

int main()
{
    int device_count = 0;
    check(cudaGetDeviceCount(&device_count), "cudaGetDeviceCount");

    std::printf("CUDA devices: %d\n", device_count);

    if (device_count < 1) {
        return 1;
    }

    cudaDeviceProp properties;
    check(
        cudaGetDeviceProperties(&properties, 0),
        "cudaGetDeviceProperties"
    );

    std::printf(
        "Device 0: %s, compute capability %d.%d\n",
        properties.name,
        properties.major,
        properties.minor
    );

    check(cudaSetDevice(0), "cudaSetDevice");
    check(cudaFree(nullptr), "CUDA context initialization");

    int *device_value = nullptr;
    check(
        cudaMalloc(&device_value, sizeof(*device_value)),
        "cudaMalloc"
    );

    set_value<<<1, 1>>>(device_value);

    check(cudaGetLastError(), "kernel launch");
    check(cudaDeviceSynchronize(), "cudaDeviceSynchronize");

    int host_value = 0;
    check(
        cudaMemcpy(
            &host_value,
            device_value,
            sizeof(host_value),
            cudaMemcpyDeviceToHost
        ),
        "cudaMemcpy"
    );

    check(cudaFree(device_value), "cudaFree");

    std::printf("Kernel result: %d\n", host_value);

    return host_value == 42 ? 0 : 1;
}
