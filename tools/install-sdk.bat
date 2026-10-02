@echo off
setlocal
set JAVA_HOME=%~dp0jdk\jdk-17.0.20.1+1
set ANDROID_SDK_ROOT=%~dp0android-sdk
set ANDROID_HOME=%~dp0android-sdk
call "%~dp0android-sdk\cmdline-tools\latest\bin\sdkmanager.bat" --sdk_root=%~dp0android-sdk --install "platform-tools" "platforms;android-34" "build-tools;34.0.0"
echo SDKMANAGER_EXIT=%ERRORLEVEL%
