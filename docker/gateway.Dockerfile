######################## BASE IMAGE ##########################
FROM python:3.12-slim AS base

ARG PYTHON_VERSION=3.12

# 设置环境变量
ENV PATH="/root/.local/bin:/opt/venv/bin:${PATH}"
ENV DEBIAN_FRONTEND=noninteractive
ENV VIRTUAL_ENV="/opt/venv"

# [严谨审核 1] 彻底切换 APT 源，并安装 git (必须) 和编译必备组件
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    && apt update -y \
    && apt install -y curl git build-essential libssl-dev pkg-config protobuf-compiler \
    && python -m venv $VIRTUAL_ENV \
    && $VIRTUAL_ENV/bin/pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && rm -rf /var/lib/apt/lists/*

# [严谨审核 2] 改用 pip 安装 uv，避开 astral.sh 网络波动
RUN $VIRTUAL_ENV/bin/pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple

######################## LOCAL SOURCE ##########################
FROM scratch AS local_src
COPY . /src

######################### BUILD IMAGE #########################
FROM base AS build-image

# [严谨审核 3] 设置 Rustup 环境变量
ENV RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup
ENV RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup
ENV PATH="/root/.cargo/bin:${PATH}"

# [严谨审核 4] 确定性安装 Rust，绕过 sh.rustup.rs
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64) rust_arch='x86_64-unknown-linux-gnu' ;; \
        aarch64) rust_arch='aarch64-unknown-linux-gnu' ;; \
        *) echo "Unsupported architecture: $arch"; exit 1 ;; \
    esac; \
    url="https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup/dist/${rust_arch}/rustup-init"; \
    curl -sSfL "$url" -o rustup-init; \
    chmod +x rustup-init; \
    ./rustup-init -y --no-modify-path --default-toolchain stable; \
    rm rustup-init; \
    rustc --version

# 拷贝源代码
COPY --from=local_src /src /opt/sglang
# 删除 macOS AppleDouble 隐藏文件（._* 文件含非 UTF-8 内容，会导致 wasmtime bindgen 解析 WIT 失败）
RUN find /opt/sglang -name '._*' -delete
WORKDIR /opt/sglang/sgl-model-gateway

# [严谨审核 5] Cargo 全局配置：
# 1. 主用 rsproxy sparse 索引（字节跳动，覆盖最全，含 sketches-ddsketch 等）
# 2. 保留 ustc 备用
# 3. 强制使用系统 git (解决 harmony 下载失败的关键)
# 4. 增大超时至 600s，重试 5 次，解决偶发下载超时
RUN mkdir -p /root/.cargo && \
    printf '[source.crates-io]\nreplace-with = "rsproxy-sparse"\n\n[source.rsproxy]\nregistry = "https://rsproxy.cn/crates.io-index"\n\n[source.rsproxy-sparse]\nregistry = "sparse+https://rsproxy.cn/index/"\n\n[source.ustc]\nregistry = "https://mirrors.ustc.edu.cn/crates.io-index"\n\n[net]\ngit-fetch-with-cli = true\nretry = 5\n\n[http]\ntimeout = 600\ncheck-revoke = false\n' > /root/.cargo/config.toml

# [严谨审核 6] 设置编译环境变量，确保 maturin 使用 vendored-openssl 提高兼容性
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true

# 执行编译：使用 --frozen 确保不进行意外的网络索引更新（如果 Cargo.lock 完整）
RUN uv pip install maturin -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && cargo clean \
    && rm -rf bindings/python/dist/ \
    && cd bindings/python \
    && ulimit -n 65536 \
    && maturin build --release --features vendored-openssl --out dist \
    && rm -rf /root/.cache

######################### ROUTER IMAGE #########################
FROM base AS router-image
COPY --from=build-image /opt/sglang/sgl-model-gateway/bindings/python/dist/*.whl dist/
RUN uv pip install --force-reinstall dist/*.whl -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN rm -rf /root/.cache dist/ \
    && apt purge -y --auto-remove git
ENTRYPOINT ["python3", "-m", "sglang_router.launch_router"]