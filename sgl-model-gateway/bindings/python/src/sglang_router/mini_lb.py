"""
Minimal HTTP load balancer for prefill and decode servers for testing.
"""

import os
import asyncio
import ipaddress
import logging
import random
import urllib
from http import HTTPStatus
from itertools import chain
from typing import Optional
import traceback

import aiohttp
import orjson
from aiorwlock import RWLock
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import ORJSONResponse, Response, StreamingResponse
from sglang_router.router_args import RouterArgs

try:
    from sglang.srt.tracing.trace import (
        process_tracing_init,
        trace_get_remote_propagate_context,
        trace_req_finish,
        trace_req_start,
        trace_set_thread_info,
        trace_slice_end,
        trace_slice_start,
    )

    trace_package_imported = True
except ImportError:
    trace_package_imported = False

# 从环境变量获取日志级别
log_level = os.getenv("LOG_LEVEL", "INFO").upper()

# 配置日志格式
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='[%(asctime)s] %(levelname)s:     %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

def str_to_bool(value, default_value=False):
    if value.lower() in ('1', 'true', 't', 'yes', 'y'):
        return True
    elif value.lower() in ('0', 'false', 'f', 'no', 'n'):
        return False
    return default_value
SGLANG_LB_LOG_EACH_CHUNK = str_to_bool(os.getenv("SGLANG_LB_LOG_EACH_CHUNK", "0"), False)

AIOHTTP_STREAM_READ_CHUNK_SIZE = (
    1024 * 64
)  # 64KB, to prevent aiohttp's "Chunk too big" error


def maybe_wrap_ipv6_address(address: str) -> str:
    try:
        ipaddress.IPv6Address(address)
        return f"[{address}]"
    except ValueError:
        return address


class MiniLoadBalancer:
    def __init__(
        self,
        router_args: RouterArgs,
    ):
        self._validate_router_args(router_args)

        self.host = router_args.host
        self.port = router_args.port
        self.timeout = router_args.request_timeout_secs
        self.prefill_urls = [url[0] for url in router_args.prefill_urls]
        self.prefill_bootstrap_ports = [url[1] for url in router_args.prefill_urls]
        self.decode_urls = router_args.decode_urls
        self.otlp_traces_endpoint = router_args.otlp_traces_endpoint
        self.enable_trace = router_args.enable_trace
        if self.enable_trace and not trace_package_imported:
            logger.warning(
                "Tracing is not supported in this environment. Please install sglang."
            )
            self.enable_trace = False
        # 添加轮询索引和读写锁
        self._prefill_index = 0
        self._decode_index = 0
        self._rw_lock = RWLock()

    def _validate_router_args(self, router_args: RouterArgs):
        logger.warning(
            "\x1b[33mMiniLB is only for debugging purposes, it only supports random policy!\033[0m"
        )

        # NOTE: too many arguments unsupported, just validate some important ones
        if router_args.policy != "random":
            logger.warning("[MiniLB] Overriding policy to random")
            router_args.policy = "random"

        if not router_args.pd_disaggregation:
            raise ValueError("MiniLB only supports PD disaggregation mode")

        if len(router_args.prefill_urls) == 0 or len(router_args.decode_urls) == 0:
            raise ValueError(
                "MiniLB requires at least one prefill and one decode server"
            )

    def start(self):
        global lb
        lb = self
        if self.enable_trace:
            process_tracing_init(self.otlp_traces_endpoint, "sglang")
            trace_set_thread_info("Mini lb")
            # 添加日志配置
        uvicorn.run(
            app,
            host=self.host,
            port=self.port,
            log_config={
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "default": {
                        "()": "uvicorn.logging.DefaultFormatter",
                        "fmt": "%(levelprefix)s %(message)s",
                        "use_colors": None,
                    },
                    "access": {
                        "()": "uvicorn.logging.AccessFormatter",
                        "fmt": '[%(asctime)s] %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
                        "datefmt": "%Y-%m-%d %H:%M:%S",
                    },
                },
                "handlers": {
                    "default": {
                        "formatter": "default",
                        "class": "logging.StreamHandler",
                        "stream": "ext://sys.stderr",
                    },
                    "access": {
                        "formatter": "access",
                        "class": "logging.StreamHandler",
                        "stream": "ext://sys.stdout",
                    },
                },
                "loggers": {
                    "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                    "uvicorn.error": {"level": "INFO"},
                    "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
                },
            }
        )

    async def select_pair(self):
        # 使用写锁，因为需要更新索引
        async with self._rw_lock.writer:
            if len(self.prefill_urls) == 0:
                raise Exception("No prefill servers available")
            if len(self.decode_urls) == 0:
                raise Exception("No decode servers available")

            # 轮询选择 prefill 服务器
            self._prefill_index %= len(self.prefill_urls)
            prefill_url = self.prefill_urls[self._prefill_index]
            bootstrap_port = self.prefill_bootstrap_ports[self._prefill_index]
            # 更新索引
            self._prefill_index = (self._prefill_index + 1) % len(self.prefill_urls)

            # 轮询选择 decode 服务器
            self._decode_index %= len(self.decode_urls)
            decode_url = self.decode_urls[self._decode_index]
            # 更新索引
            self._decode_index = (self._decode_index + 1) % len(self.decode_urls)

            logger.info(
                f"Selected pair: prefill={prefill_url}, decode={decode_url}"
            )
            return prefill_url, bootstrap_port, decode_url


    async def generate(
        self, modified_request, prefill_server, decode_server, endpoint
    ) -> ORJSONResponse:
        assert endpoint[0] != "/", f"Endpoint should not start with '/': {endpoint}"

        # 检查 n 参数必须为 1
        if modified_request.get("n") is not None:
            n = modified_request.get("n")
            if not isinstance(n, int) or n != 1:
                return ORJSONResponse(
                    content={
                        "object": "error",
                        "message": "Not support n != 1.",
                        "type": "BadRequestError",
                        "param": None,
                        "code": 400
                    },
                    status_code=400,
                )

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=self.timeout
            )  # Add timeout for request reliability
        ) as session:
            headers = {}
            bootstrap_room_list = []
            if self.enable_trace:
                bootstrap_room_list = (
                    modified_request["bootstrap_room"]
                    if isinstance(modified_request["bootstrap_room"], list)
                    else [modified_request["bootstrap_room"]]
                )
                trace_context = trace_get_remote_propagate_context(bootstrap_room_list)
                headers = {"trace_context": trace_context}

            tasks = [
                session.post(
                    f"{prefill_server}/{endpoint}",
                    json=modified_request,
                    headers=headers,
                ),
                session.post(
                    f"{decode_server}/{endpoint}",
                    json=modified_request,
                    headers=headers,
                ),
            ]

            for bootstrap_room in bootstrap_room_list:
                trace_slice_end("mini_lb_launch", bootstrap_room, auto_next_anon=True)

            # Wait for both responses to complete. Prefill should end first.
            prefill_response, decode_response = await asyncio.gather(*tasks)

            if "return_logprob" in modified_request:

                prefill_json = await prefill_response.json()
                ret_json = await decode_response.json()

                # merge `meta_info.input_token_logprobs` from prefill to decode
                if "meta_info" in ret_json:
                    if "input_token_logprobs" in ret_json["meta_info"]:
                        ret_json["meta_info"]["input_token_logprobs"] = (
                            prefill_json["meta_info"]["input_token_logprobs"]
                            + ret_json["meta_info"]["input_token_logprobs"]
                        )
            else:
                ret_json = await decode_response.json()

            for bootstrap_room in bootstrap_room_list:
                trace_slice_end(
                    "wait_PD_finish",
                    bootstrap_room,
                    thread_finish_flag=True,
                )
                trace_req_finish(bootstrap_room)

            return ORJSONResponse(
                content=ret_json,
                status_code=decode_response.status,
            )

    async def generate_stream(
        self, modified_request, prefill_server, decode_server, endpoint="generate"
    ):
        assert endpoint[0] != "/", f"Endpoint should not start with '/': {endpoint}"

        # 设置 return_logprob 永远为 False
        modified_request["return_logprob"] = False

        async def stream_results():
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session:
                # Create the tasks for both prefill and decode requests
                headers = {}
                bootstrap_room_list = []
                if self.enable_trace:
                    bootstrap_room_list = (
                        modified_request["bootstrap_room"]
                        if isinstance(modified_request["bootstrap_room"], list)
                        else [modified_request["bootstrap_room"]]
                    )
                    trace_context = trace_get_remote_propagate_context(
                        bootstrap_room_list
                    )
                    headers = {"trace_context": trace_context}

                tasks = [
                    session.post(
                        f"{prefill_server}/{endpoint}",
                        json=modified_request,
                        headers=headers,
                    ),
                    session.post(
                        f"{decode_server}/{endpoint}",
                        json=modified_request,
                        headers=headers,
                    ),
                ]

                for bootstrap_room in bootstrap_room_list:
                    trace_slice_end(
                        "mini_lb_launch", bootstrap_room, auto_next_anon=True
                    )
                # Wait for both responses to complete
                prefill_response, decode_response = await asyncio.gather(*tasks)

                # 尝试获取第一个chunk
                first_chunk_bytes_received = False
                async for chunk_bytes in decode_response.content:
                    if not first_chunk_bytes_received:
                        first_chunk_bytes_received = True

                        # 检查第一个chunk的内容
                        first_chunk_str = chunk_bytes.decode('utf-8')

                        # 记录 prefill 和 decode 服务器信息
                        logger.info(f"Streaming from {prefill_server=}, {decode_server=}, {first_chunk_str=}")

                        # 如果第一个chunk是 [DONE]，抛出异常
                        if (first_chunk_str.strip() == "data: [DONE]") or (first_chunk_str.strip() == "[DONE]"):
                            raise Exception("First chunk is [DONE]")

                        # 移除可能的 "data: " 前缀
                        if first_chunk_str.startswith("data: "):
                            data_str = first_chunk_str[6:].strip()
                        else:
                            data_str = first_chunk_str.strip()

                        # 如果为空数据，抛出异常
                        if not data_str:
                            raise Exception("Empty first chunk")

                        # 解析JSON数据
                        try:
                            data = orjson.loads(data_str)

                            # 检查是否是错误响应
                            if data.get("object") == "error":
                                status_code = data.get("code", 500)
                                yield status_code, chunk_bytes
                                break

                            if data.get("error") != None:
                                status_code = data.get("error").get("code", 500)
                                yield status_code, chunk_bytes
                                break

                        except Exception as e:
                            raise Exception(f"Failed to parse first chunk: {str(e)}. First chunk is {first_chunk_str}")

                        # 第一次返回状态码和chunk
                        yield decode_response.status, chunk_bytes
                        break  # 只检查第一个chunk

                # 如果未成功接收第一个chunk，抛出异常
                if not first_chunk_bytes_received:
                    raise Exception("No data chunks received from decode server")

                # 后续只返回chunk
                async for chunk_bytes in decode_response.content.iter_chunked(
                    AIOHTTP_STREAM_READ_CHUNK_SIZE
                ):
                    yield chunk_bytes

            for bootstrap_room in bootstrap_room_list:
                trace_slice_end(
                    "wait_PD_finish",
                    bootstrap_room,
                    thread_finish_flag=True,
                )
                trace_req_finish(bootstrap_room)

        try:
            # 尝试获取第一个chunk以确保连接正常
            stream_iterator = stream_results()
            first_result = await stream_iterator.__anext__()

            # 处理第一次返回的两个对象
            status_code, first_chunk_bytes = first_result

            async def full_stream():
                yield first_chunk_bytes

                # 处理后续只返回chunk的情况
                async for chunk_bytes in stream_iterator:
                    # 根据环境变量控制是否记录每个chunk
                    if SGLANG_LB_LOG_EACH_CHUNK:
                        logger.info(f"Chunk: {chunk_bytes}")
                    yield chunk_bytes

            return StreamingResponse(
                full_stream(),
                media_type="text/event-stream",
                status_code=status_code
            )
        except Exception as e:
            logger.error(f"Error in stream generation: {e}")
            traceback.print_exc()
            return StreamingResponse(
                iter([b""]),
                media_type="text/event-stream",
                status_code=500
            )




app = FastAPI()
lb: Optional[MiniLoadBalancer] = None


@app.get("/health")
async def health_check():
    return Response(status_code=200)


@app.get("/health_generate")
async def health_generate():
    async with aiohttp.ClientSession() as session:
        # Create the tasks
        tasks = []
        for server in chain(lb.prefill_urls, lb.decode_urls):
            tasks.append(session.get(f"{server}/health_generate"))
        for i, response in enumerate(asyncio.as_completed(tasks)):
            await response
    return Response(status_code=200)


@app.post("/flush_cache")
async def flush_cache():
    async with aiohttp.ClientSession() as session:
        # Create the tasks
        tasks = []
        for server in chain(lb.prefill_urls, lb.decode_urls):
            tasks.append(session.post(f"{server}/flush_cache"))
        for i, response in enumerate(asyncio.as_completed(tasks)):
            await response
    return Response(status_code=200)


@app.get("/get_server_info")
async def get_server_info():
    prefill_infos = []
    decode_infos = []
    all_internal_states = []

    async with aiohttp.ClientSession() as session:
        for server in lb.prefill_urls:
            server_info = await session.get(f"{server}/get_server_info")
            prefill_infos.append(await server_info.json())
        for server in lb.decode_urls:
            server_info = await session.get(f"{server}/get_server_info")
            info_json = await server_info.json()
            decode_infos.append(info_json)
            # Extract internal_states from decode servers
            if "internal_states" in info_json:
                all_internal_states.extend(info_json["internal_states"])

    # Return format expected by bench_one_batch_server.py
    if all_internal_states:
        return {
            "internal_states": all_internal_states,
            "prefill": prefill_infos,
            "decode": decode_infos,
        }
    else:
        # Fallback with dummy data if no internal states found
        return {
            "internal_states": [
                {
                    "last_gen_throughput": 0.0,
                    "avg_spec_accept_length": None,
                }
            ],
            "prefill": prefill_infos,
            "decode": decode_infos,
        }


async def _get_model_info_impl():
    if not lb or not lb.prefill_urls:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="There is no server registered",
        )

    target_server_url = lb.prefill_urls[0]
    endpoint_url = f"{target_server_url}/model_info"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(endpoint_url) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise HTTPException(
                        status_code=HTTPStatus.BAD_GATEWAY,
                        detail=(
                            f"Failed to get model info from {target_server_url}"
                            f"Status: {response.status}, Response: {error_text}"
                        ),
                    )

                model_info_json = await response.json()
                return ORJSONResponse(content=model_info_json)

        except aiohttp.ClientError as e:
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                detail=f"Failed to get model info from backend",
            )


@app.get("/model_info")
async def model_info():
    return await _get_model_info_impl()


@app.get("/get_model_info")
async def get_model_info():
    return await _get_model_info_impl()


@app.post("/generate")
async def handle_generate_request(request_data: dict):
    prefill_server, bootstrap_port, decode_server = await lb.select_pair()

    # Parse and transform prefill_server for bootstrap data
    parsed_url = urllib.parse.urlparse(prefill_server)
    hostname = maybe_wrap_ipv6_address(parsed_url.hostname)
    modified_request = request_data.copy()

    batch_size = _get_request_batch_size(modified_request)
    if batch_size is not None:
        modified_request.update(
            {
                "bootstrap_host": [hostname] * batch_size,
                "bootstrap_port": [bootstrap_port] * batch_size,
                "bootstrap_room": [
                    _generate_bootstrap_room() for _ in range(batch_size)
                ],
            }
        )
    else:
        modified_request.update(
            {
                "bootstrap_host": hostname,
                "bootstrap_port": bootstrap_port,
                "bootstrap_room": _generate_bootstrap_room(),
            }
        )

    if request_data.get("stream", False):
        return await lb.generate_stream(
            modified_request, prefill_server, decode_server, "generate"
        )
    else:
        return await lb.generate(
            modified_request, prefill_server, decode_server, "generate"
        )


async def _forward_to_backend(request_data: dict, endpoint_name: str):
    prefill_server, bootstrap_port, decode_server = await lb.select_pair()

    # Parse and transform prefill_server for bootstrap data
    parsed_url = urllib.parse.urlparse(prefill_server)
    hostname = maybe_wrap_ipv6_address(parsed_url.hostname)
    modified_request = request_data.copy()
    modified_request.update(
        {
            "bootstrap_host": hostname,
            "bootstrap_port": bootstrap_port,
            "bootstrap_room": _generate_bootstrap_room(),
        }
    )

    if request_data.get("stream", False):
        return await lb.generate_stream(
            modified_request,
            prefill_server,
            decode_server,
            endpoint=endpoint_name,
        )
    else:
        return await lb.generate(
            modified_request,
            prefill_server,
            decode_server,
            endpoint=endpoint_name,
        )


@app.post("/v1/chat/completions")
async def handle_chat_completion_request(request_data: dict):
    return await _forward_to_backend(request_data, "v1/chat/completions")


@app.post("/v1/completions")
async def handle_completion_request(request_data: dict):
    return await _forward_to_backend(request_data, "v1/completions")


def _generate_bootstrap_room():
    bootstrap_room = random.randint(0, 2**63 - 1)
    if lb.enable_trace:
        trace_req_start(bootstrap_room, bootstrap_room, role="router")
        trace_slice_start("mini_lb_launch", bootstrap_room)
    return bootstrap_room


# We may utilize `GenerateReqInput`'s logic later
def _get_request_batch_size(request):
    if (text := request.get("text")) is not None:
        return None if isinstance(text, str) else len(text)
    if (input_ids := request.get("input_ids")) is not None:
        return None if isinstance(input_ids[0], int) else len(input_ids)
    return None


@app.get("/v1/models")
async def get_models():
    prefill_server = lb.prefill_urls[0]  # Get the first prefill server
    async with aiohttp.ClientSession() as session:
        try:
            response = await session.get(f"{prefill_server}/v1/models")
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Prefill server error: Status {response.status}",
                )
            return ORJSONResponse(content=await response.json())
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
