#!/usr/bin/env python3

import gzip
import io
import json
import logging
import lzma
import os
import re
import requests
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import apt_pkg
from apt_pkg import version_compare

apt_pkg.init()  # 初始化 apt_pkg

arch_List = ["amd64", "arm64", "all", "i386"]
infoList = {arch: {} for arch in arch_List} # 存放包的信息
""" infoList format:
{
    "arch": {
        "pkg_name": {
            "version": "1.0.0",
            "url": "https://example.com/pkg_name.deb",
            "package": ""
        }
    }
}
"""
lock = {arch: Lock() for arch in arch_List}

USER_AGENT = "Debian APT-HTTP/1.3 (3.0.3)"  # from Debian 13

def read_repo_list(repo_list_file: str) -> dict:
    """
    repo info json format:
    "repo_name": {
        "repo": repo url, end with "/" is better
        "path": {
            "arch": repo Packages file path of "arch", don't start with "/"
        }
    }
    """
    try:
        with open(repo_list_file, "r") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error reading repo list: {e}")
        return {}


def get_remote_packages(repo_url: str, file_path: str) -> bytes:
    """
    get the packages file content from remote repo
    """
    file_url = os.path.join(repo_url, file_path)
    try:
        response = requests.get(
            file_url, timeout=10, headers={"User-Agent": USER_AGENT}
        )
        if response.status_code != 200:
            logging.error(
                f"GetError: {file_url} returned status {response.status_code}"
            )
            return b""

        content = b""
        if file_url.endswith(".gz"):  # Packages.gz
            with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as f:
                content = f.read()
        elif file_url.endswith(".xz"):  # Packages.xz
            with lzma.LZMAFile(io.BytesIO(response.content)) as f:
                content = f.read()
        else:  # Packages
            content = response.content

        return content.replace(b"Filename: ", f"Filename: {repo_url}".encode())
    except Exception as e:
        logging.error(f"Error fetching packages: {e}")
        return b""


def split_latest(packages_file_content: bytes):
    """
    split the information of each packet, deduplication and store the latest in infoList
    将每个包的信息分割开，去重并将最新的存放到 infoList 中
    """
    # Remove trailing empty lines first
    packages_file_content = packages_file_content.rstrip(b"\n\r\t ")

    # split on two or more consecutive blank lines
    package_list = [
        part + b"\n\n"
        for part in re.split(rb"(?:\r?\n){2,}", packages_file_content)
        if part.strip()
    ]

    find_name = re.compile(rb"Package:[ ]*(.+)")
    find_arch = re.compile(rb"Architecture:[ ]*(.+)")
    find_url = re.compile(rb"Filename:[ ]*(.+)")
    find_version = re.compile(rb"Version:[ ]*(.+)")

    for package in package_list:
        name = "unknown"
        try:
            name = find_name.search(package).group(1).decode()
            arch = find_arch.search(package).group(1).decode()
            url = find_url.search(package).group(1).decode()
            tmp_version = find_version.search(package).group(1).decode()
            with lock[arch]:
                # 使用 apt_pkg 进行版本比较
                if (
                    name not in infoList[arch]
                    or version_compare(tmp_version, infoList[arch][name]["version"]) > 0
                ):
                    infoList[arch][name] = {"version": tmp_version, "url": url, "package": package}
        except Exception as e:
            logging.error(f"Error processing package {name}: {e}")
    return


def process_repo(r: dict):
    """
    获取仓库中不同架构子仓库的内容，最后调用 get_latest 去重并保存。
    """
    try:
        for path in r["path"].values():
            split_latest(get_remote_packages(r["repo"], path))
    except Exception as e:
        logging.error(f"Error processing repo {r.get('name', 'unknown')}: {e}")



if __name__ == "__main__":
    # 读取 repo_list 配置
    repo_list = read_repo_list("data/apt-repo.json")
    if not repo_list:
        sys.exit()

    # 多线程，同时限制最大线程数
    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(process_repo, repo_list.values())

    # 分别输出到不同文件
    for arch in ["amd64", "arm64", "all"]:
        os.makedirs(f"packages/{arch}/", exist_ok=True)
        for (name, info) in infoList[arch].items():
            with open(f"packages/{arch}/{name}.package", "+wb") as f:
                f.write(info["package"])
