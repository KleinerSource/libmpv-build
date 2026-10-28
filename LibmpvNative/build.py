#!/usr/bin/env python3
"""独立构建 libmpv；App 构建不调用本文件。"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = ROOT / "build/libmpv-native"
DIST = WORK / "dist"
LOCK = json.loads((HERE / "sources.lock.json").read_text(encoding="utf-8"))
ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
PLACEBO_OPTIONS = ["-Dopengl=enabled", "-Dvulkan=disabled", "-Dd3d11=disabled",
                   "-Dshaderc=disabled", "-Dglslang=disabled", "-Dlcms=disabled",
                   "-Ddovi=disabled", "-Dlibdovi=disabled", "-Dunwind=disabled",
                   "-Dxxhash=disabled", "-Ddemos=false", "-Dtests=false"]


def run(args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def replace(path, before, after):
    text = path.read_text(encoding="utf-8")
    if text.count(before) != 1:
        raise ValueError(f"源码补丁上下文不匹配: {path}: {before!r}")
    path.write_text(text.replace(before, after), encoding="utf-8")


def source(name, destination=None):
    spec = LOCK["sources"][name]
    path = destination or WORK / "sources" / name
    if path.exists():
        # 不重用有补丁的源码，防止在旧补丁上重复修改。
        raise ValueError(f"源码目录已存在，请使用新的构建目录: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", path])
    run(["git", "-C", path, "fetch", "--depth=1", spec["url"], spec["commit"]])
    run(["git", "-C", path, "checkout", "--detach", "FETCH_HEAD"])
    actual = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if actual != spec["commit"]:
        raise ValueError(f"源码提交不匹配: {name}")
    if name == "mpv":
        run(["git", "-C", path, "tag", "v" + LOCK["version"]])
    return path


def licenses(roots):
    result = {}
    for name, root in roots.items():
        for pattern in ("LICENSE*", "COPYING*", "LICENCE*", "docs/FTL.TXT", "docs/GPLv2.TXT"):
            for path in root.glob(pattern):
                if path.is_file():
                    result[f"{name}/{path.name}"] = path.read_bytes()
    return result


def patch_mpv(path, platform):
    if (path / "MPV_VERSION").read_text(encoding="utf-8").strip() != LOCK["version"]:
        raise ValueError("mpv 源码版本与锁定版本不一致")
    if platform == "android":
        with (path / "include/mpv/client.h").open("a", encoding="utf-8") as output:
            output.write("\nMPV_EXPORT int mpv_lavc_set_java_vm(void *vm);\n")
        with (path / "player/client.c").open("a", encoding="utf-8") as output:
            output.write("\n#include <libavcodec/jni.h>\n"
                         "int mpv_lavc_set_java_vm(void *vm) { return av_jni_set_java_vm(vm, NULL); }\n")
    else:
        # 保留 media-kit 原有的多实例音频会话引用计数与跳过管理选项。
        run(["git", "-C", path, "apply", HERE / "patches/audiounit-shared-session.patch"])


def check_ffmpeg_config(path):
    config = "\n".join((path / name).read_text(encoding="utf-8")
                       for name in ("config.h", "config_components.h"))
    for feature in ("CONFIG_H264_DECODER", "CONFIG_HEVC_DECODER", "CONFIG_HDMV_PGS_SUBTITLE_DECODER"):
        if f"#define {feature} 1" not in config:
            raise ValueError(f"FFmpeg 缺少解码器: {feature}")


def patch_ffmpeg(path):
    # 6.1.4 已包含 HLS MP4 init-section seek 修复，仅移植 DASH URL 转义修复。
    if "pls->cur_init_section = NULL;" not in (path / "libavformat/hls.c").read_text(encoding="utf-8"):
        raise ValueError("FFmpeg 缺少 HLS MP4 seek 修复")
    dash = path / "libavformat/dashdec.c"
    replace(dash, "        xmlNodeSetContent(node, root_url);",
            "        char *content = xmlEncodeSpecialChars(NULL, root_url);\n"
            "        xmlNodeSetContent(node, content);\n        xmlFree(content);")
    replace(dash, "            xmlNodeSetContent(baseurl_nodes[i], tmp_str);",
            "            char *content = xmlEncodeSpecialChars(NULL, tmp_str);\n"
            "            xmlNodeSetContent(baseurl_nodes[i], content);\n            xmlFree(content);")


def android():
    upstream = source("android-build")
    scripts = upstream / "buildscripts"
    depinfo = scripts / "include/depinfo.sh"
    replace(depinfo, "v_ndk=25.2.9519653", f"v_ndk={LOCK['android_ndk']}")
    replace(depinfo, "dep_mpv=(ffmpeg libass)", "dep_mpv=(ffmpeg libass libplacebo)")
    for name in ("mbedtls", "dav1d", "freetype", "fribidi", "harfbuzz", "libass", "libxml2"):
        source(name, scripts / "deps" / name)
    source("android-helper", scripts / "deps/media-kit-android-helper")
    source("ffmpeg", scripts / "deps/ffmpeg")
    mpv = source("mpv", scripts / "deps/mpv")
    source("libplacebo", scripts / "deps/libplacebo")
    sdk = Path(os.environ["ANDROID_HOME"])
    (scripts / "sdk").mkdir()
    (scripts / "sdk/android-sdk-linux").symlink_to(sdk, target_is_directory=True)
    patch_ffmpeg(scripts / "deps/ffmpeg")
    patch_mpv(mpv, "android")
    shutil.copyfile(scripts / "flavors/full.sh", scripts / "scripts/ffmpeg.sh")
    placebo_script = '''#!/bin/bash -e
. ../../include/depinfo.sh
. ../../include/path.sh
unset CC CXX
meson setup _build$ndk_suffix --cross-file "$prefix_dir/crossfile.txt" --prefix=/usr/local --libdir=lib --default-library=static --buildtype=release --wrap-mode=nodownload OPTIONS
meson compile -C _build$ndk_suffix -j "$cores"
DESTDIR="$prefix_dir" meson install -C _build$ndk_suffix
'''.replace("OPTIONS", " ".join(PLACEBO_OPTIONS))
    (scripts / "scripts/libplacebo.sh").write_text(placebo_script, encoding="utf-8")
    replace(scripts / "scripts/mpv.sh", "  \t-Dlibplacebo=disabled \\\n", "")
    # 上游交叉文件缺少 CFLAGS/LDFLAGS，显式传入 16 KiB page-size 链接要求。
    replace(scripts / "scripts/mpv.sh", "-Dgpl=false", "-Dgpl=false -Dc_link_args=-Wl,-z,max-page-size=16384")
    run(["bash", "build.sh"], cwd=scripts)
    for abi in ABIS:
        suffix = {"arm64-v8a": "-arm64", "armeabi-v7a": "", "x86_64": "-x64", "x86": "-x86"}[abi]
        check_ffmpeg_config(scripts / f"deps/ffmpeg/_build{suffix}")
    helper = scripts / "deps/media-kit-android-helper"
    replace(helper / "app/build.gradle", "    compileSdk 35", f"    compileSdk 35\n    ndkVersion '{LOCK['android_ndk']}'")
    run(["bash", "gradlew", "assembleRelease"], cwd=helper)
    notices = licenses({p.name: p for p in (scripts / "deps").iterdir() if p.is_dir()})
    with zipfile.ZipFile(helper / "app/build/outputs/apk/release/app-release.apk") as apk:
        for abi in ABIS:
            library = scripts / "prefix" / abi / "lib/libmpv.so"
            tools = sdk / "ndk" / LOCK["android_ndk"] / "toolchains/llvm/prebuilt/linux-x86_64/bin"
            run([tools / "llvm-strip", "--strip-unneeded", library])
            symbols = subprocess.check_output([tools / "llvm-nm", "-D", "--defined-only", library], text=True)
            for symbol in ("mpv_create", "mpv_render_context_create", "mpv_lavc_set_java_vm"):
                if symbol not in symbols:
                    raise ValueError(f"Android 缺少符号: {symbol}")
            with zipfile.ZipFile(DIST / f"full-{abi}.jar", "w", zipfile.ZIP_DEFLATED) as jar:
                for name in apk.namelist():
                    if name.startswith(f"lib/{abi}/") and name.endswith(".so") and not name.endswith("/libmpv.so"):
                        jar.writestr(name, apk.read(name))
                jar.write(library, f"lib/{abi}/libmpv.so")
                jar.writestr("META-INF/libmpv-sources.json", json.dumps(LOCK, indent=2))
                for name, data in notices.items():
                    jar.writestr("META-INF/licenses/" + name, data)


def ios(jobs):
    spec = importlib.util.spec_from_file_location("ios_native", HERE / "ios_deps.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    # 从官方上游源码构建独立依赖，不使用 App 的 KSPlayer/FFmpegKit 组件。
    native.WORK = WORK / "ios-deps"
    native.prepare()
    mpv = source("mpv")
    placebo = source("libplacebo")
    patch_mpv(mpv, "ios")
    exports = WORK / "mpv.exports"
    exports.write_text("_mpv_*\n", encoding="utf-8")
    frameworks = {}
    for sdk, arch in native.SLICES:
        prefix = native.build_slice(sdk, arch, jobs)
        work = native.WORK / "slices" / f"{sdk}-{arch}"
        check_ffmpeg_config(work / "ffmpeg")
        env, _, _ = native.slice_environment(sdk, arch, prefix)
        cross = work / "cross.meson"
        for name, src, options in (
            ("libplacebo", placebo, ["--default-library=static", *PLACEBO_OPTIONS]),
            ("mpv", mpv, ["--default-library=shared", "-Dgpl=true", "-Dlibmpv=true", "-Dcplayer=false",
                          "-Dlua=disabled", "-Djavascript=disabled", "-Dcocoa=disabled", "-Dswift-build=disabled",
                          "-Dcoreaudio=disabled", "-Davfoundation=disabled", "-Daudiounit=enabled",
                          "-Dgl=enabled", "-Dplain-gl=enabled", "-Dios-gl=enabled", "-Dvulkan=disabled",
                          "-Dgl-cocoa=disabled", "-Dmanpage-build=disabled",
                          f"-Dc_link_args={env['LDFLAGS']} -framework OpenGLES -framework AVFoundation -Wl,-exported_symbols_list,{exports}"]),
        ):
            run(["meson", "setup", work / name, src, "--cross-file", cross, "--prefix", prefix,
                 "--libdir=lib", "--buildtype=release", "--wrap-mode=nodownload", "--prefer-static", *options], env=env)
            run(["meson", "compile", "-C", work / name, "-j", jobs], env=env)
            run(["meson", "install", "-C", work / name], env=env)
        framework = work / "Mpv.framework"
        (framework / "Headers").mkdir(parents=True)
        for header in (mpv / "include/mpv").glob("*.h"):
            shutil.copyfile(header, framework / "Headers" / header.name)
        (framework / "Headers/Mpv.h").write_text(
            "".join(f'#include "{p.name}"\n' for p in sorted((mpv / "include/mpv").glob("*.h"))), encoding="utf-8")
        (framework / "Modules").mkdir()
        (framework / "Modules/module.modulemap").write_text(
            'framework module Mpv { umbrella header "Mpv.h" export * }\n', encoding="utf-8")
        shutil.copy2(prefix / "lib/libmpv.dylib", framework / "Mpv")
        run(["install_name_tool", "-id", "@rpath/Mpv.framework/Mpv", framework / "Mpv"])
        loads = subprocess.check_output(["otool", "-L", framework / "Mpv"], text=True)
        for line in loads.splitlines()[1:]:
            dependency = line.strip().split(" ")[0]
            if not dependency.startswith(("@rpath/Mpv.framework/", "/System/Library/", "/usr/lib/")):
                raise ValueError(f"iOS 非系统动态依赖泄漏: {dependency}")
        symbols = subprocess.check_output(["nm", "-gU", framework / "Mpv"], text=True)
        names = [line.split()[-1] for line in symbols.splitlines() if line.split()]
        if not {"_mpv_create", "_mpv_render_context_create"}.issubset(names) or any(not n.startswith("_mpv_") for n in names):
            raise ValueError("iOS 导出符号不符合 mpv 隔离契约")
        with (framework / "Info.plist").open("wb") as output:
            plistlib.dump({"CFBundleIdentifier": "org.ohmymedia.mpv", "CFBundleExecutable": "Mpv",
                           "CFBundleName": "Mpv", "CFBundlePackageType": "FMWK", "CFBundleVersion": "1",
                           "CFBundleShortVersionString": LOCK["version"], "MinimumOSVersion": "16.0",
                           "CFBundleSupportedPlatforms": ["iPhoneOS" if sdk == "iphoneos" else "iPhoneSimulator"]}, output)
        frameworks[sdk, arch] = framework
    simulator = frameworks["iphonesimulator", "arm64"]
    run(["lipo", "-create", simulator / "Mpv", frameworks["iphonesimulator", "x86_64"] / "Mpv",
         "-output", WORK / "Mpv-simulator"])
    shutil.copyfile(WORK / "Mpv-simulator", simulator / "Mpv")
    bundle = WORK / "libmpv/Mpv.xcframework"
    bundle.parent.mkdir()
    run(["xcodebuild", "-create-xcframework", "-framework", frameworks["iphoneos", "arm64"],
         "-framework", simulator, "-output", bundle])
    roots = {p.name: p for p in (native.WORK / "sources").iterdir() if p.is_dir()}
    roots.update(mpv=mpv, libplacebo=placebo)
    for name, data in licenses(roots).items():
        target = bundle.parent / "Licenses" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    with tarfile.open(DIST / "libmpv-ios.tar.gz", "w:gz") as archive:
        archive.add(bundle.parent, arcname="libmpv")


def manifest(directory):
    expected = {"libmpv-ios.tar.gz", *(f"full-{abi}.jar" for abi in ABIS)}
    if not all((directory / name).is_file() for name in expected):
        raise ValueError("发布必须同时包含 iOS 和全部 Android ABI")
    data = {"version": LOCK["version"], "release": LOCK["release"], "sources": LOCK["sources"],
            "assets": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in sorted(expected)}}
    (directory / "manifest.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("android", "ios", "manifest"))
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    DIST.mkdir(parents=True, exist_ok=True)
    if args.platform == "android":
        android()
    elif args.platform == "ios":
        ios(args.jobs)
    else:
        manifest(DIST)
