[app]
title = ArgonGram
package.name = argongram
package.domain = org.argon
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 2.1
requirements = python3,kivy
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE
android.archs = arm64-v8a
android.allow_backup = True

android.api = 33
android.minapi = 24
android.ndk = 25b
android.accept_sdk_license = True
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 0
