"""从官方源码构建 libmpv 的 iOS 静态依赖，与 App 原生组件完全独立。"""

import os
import shlex
import subprocess

from build import patch_ffmpeg, run, source

WORK = None  # 由独立组件构建器设置。
SLICES = (("iphoneos", "arm64"), ("iphonesimulator", "arm64"), ("iphonesimulator", "x86_64"))
ORDER = ("mbedtls", "dav1d", "freetype", "fribidi", "harfbuzz", "libass", "ffmpeg")


def prepare():
    for name in ORDER:
        path = source(name, WORK / "sources" / name)
        if name == "libass":
            run(["autoreconf", "-fi"], cwd=path)
        elif name == "ffmpeg":
            patch_ffmpeg(path)


def slice_environment(sdk, arch, prefix):
    sdkroot = subprocess.check_output(["xcrun", "--sdk", sdk, "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--sdk", sdk, "--find", "clang"], text=True).strip()
    target = f"{arch}-apple-ios16.0" + ("-simulator" if sdk == "iphonesimulator" else "")
    flags = ["-target", target, "-isysroot", sdkroot, "-O2", "-fPIC", "-I" + str(prefix / "include")]
    env = os.environ.copy()
    env.update(CC=clang, CXX=clang + "++", OBJC=clang, AR="/usr/bin/ar", RANLIB="/usr/bin/ranlib",
               CFLAGS=shlex.join(flags), CXXFLAGS=shlex.join(flags + ["-stdlib=libc++"]),
               CPPFLAGS=shlex.join(flags), LDFLAGS=shlex.join(flags + ["-L" + str(prefix / "lib")]),
               PKG_CONFIG_PATH="", PKG_CONFIG_LIBDIR=str(prefix / "lib/pkgconfig"), PKG_CONFIG_SYSROOT_DIR="",
               SDKROOT="", MACOSX_DEPLOYMENT_TARGET="", IPHONEOS_DEPLOYMENT_TARGET="")
    return env, flags, sdkroot


def meson_cross(path, env, flags, arch):
    quote = lambda text: "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"
    args = ", ".join(map(quote, flags))
    cpp_args = ", ".join(map(quote, flags + ["-stdlib=libc++"]))
    path.write_text(f"""[binaries]
c = {quote(env['CC'])}
cpp = {quote(env['CXX'])}
objc = {quote(env['CC'])}
objcpp = {quote(env['CXX'])}
ar = '/usr/bin/ar'
strip = '/usr/bin/strip'
pkg-config = 'pkg-config'
[host_machine]
system = 'darwin'
cpu_family = '{'aarch64' if arch == 'arm64' else 'x86_64'}'
cpu = '{arch}'
endian = 'little'
[properties]
needs_exe_wrapper = true
[built-in options]
c_args = [{args}]
cpp_args = [{cpp_args}]
objc_args = [{args}]
objcpp_args = [{cpp_args}]
c_link_args = [{args}, '-L{path.parent / 'prefix/lib'}']
cpp_link_args = [{cpp_args}, '-L{path.parent / 'prefix/lib'}']
""", encoding="utf-8")


def build_slice(sdk, arch, jobs):
    work = WORK / "slices" / f"{sdk}-{arch}"
    prefix = work / "prefix"
    prefix.mkdir(parents=True)
    env, flags, sdkroot = slice_environment(sdk, arch, prefix)
    cross = work / "cross.meson"
    meson_cross(cross, env, flags, arch)
    for name in ORDER:
        src = WORK / "sources" / name
        build = work / name
        if name in ("dav1d", "freetype", "fribidi", "harfbuzz"):
            options = {
                "dav1d": ["-Denable_tools=false", "-Denable_tests=false", "-Denable_examples=false"],
                "freetype": ["-Dbrotli=disabled", "-Dharfbuzz=disabled", "-Dpng=disabled", "-Dbzip2=disabled"],
                "fribidi": ["-Ddocs=false", "-Dtests=false"],
                "harfbuzz": ["-Dglib=disabled", "-Dgobject=disabled", "-Dicu=disabled", "-Ddocs=disabled",
                             "-Dtests=disabled", "-Dcairo=disabled", "-Dintrospection=disabled", "-Dfreetype=enabled"],
            }[name]
            run(["meson", "setup", build, src, "--cross-file", cross, "--prefix", prefix,
                 "--libdir=lib", "--default-library=static", "--buildtype=release", "--wrap-mode=nodownload", *options], env=env)
            run(["meson", "compile", "-C", build, "-j", jobs], env=env)
            run(["meson", "install", "-C", build], env=env)
        elif name == "mbedtls":
            run(["cmake", "-S", src, "-B", build, "-DCMAKE_SYSTEM_NAME=iOS",
                 f"-DCMAKE_OSX_SYSROOT={sdkroot}", f"-DCMAKE_OSX_ARCHITECTURES={arch}",
                 "-DCMAKE_OSX_DEPLOYMENT_TARGET=16.0", f"-DCMAKE_INSTALL_PREFIX={prefix}",
                 "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
                 "-DENABLE_TESTING=OFF", "-DENABLE_PROGRAMS=OFF", "-DUSE_SHARED_MBEDTLS_LIBRARY=OFF",
                 "-DMBEDTLS_FATAL_WARNINGS=OFF"], env=env)
            run(["cmake", "--build", build, "--parallel", jobs], env=env)
            run(["cmake", "--install", build], env=env)
        else:
            build.mkdir()
            if name == "libass":
                host = "aarch64-apple-darwin" if arch == "arm64" else "x86_64-apple-darwin"
                options = [f"--prefix={prefix}", f"--host={host}", "--enable-static", "--disable-shared",
                           "--disable-fontconfig", "--disable-require-system-font-provider", "--disable-test", "--with-pic"]
            else:
                options = [f"--prefix={prefix}", "--target-os=darwin", "--enable-cross-compile", f"--arch={arch}",
                           f"--cc={env['CC']}", f"--sysroot={sdkroot}", f"--extra-cflags={env['CFLAGS']}",
                           f"--extra-ldflags={env['LDFLAGS']}", "--pkg-config=pkg-config", "--pkg-config-flags=--static",
                           "--enable-static", "--disable-shared", "--enable-pic", "--disable-programs", "--disable-doc",
                           "--disable-debug", "--disable-autodetect", "--disable-devices", "--disable-avdevice",
                           "--enable-version3", "--enable-network",
                           "--enable-videotoolbox", "--enable-audiotoolbox", "--enable-mbedtls", "--enable-libdav1d",
                           "--enable-libass", "--enable-zlib", "--enable-bzlib", "--enable-iconv"]
            run([src / "configure", *options], cwd=build, env=env)
            run(["make", f"-j{jobs}"], cwd=build, env=env)
            run(["make", "install"], cwd=build, env=env)
    return prefix
