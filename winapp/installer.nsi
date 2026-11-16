Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APP_NAME "Fiber Voice"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\FiberVoice"
Name "${APP_NAME}"
OutFile "dist\FiberVoice-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Fiber Voice"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
RequestExecutionLevel user
ManifestDPIAware true
SetCompressor /SOLID lzma
Icon "AppIcon.ico"
UninstallIcon "AppIcon.ico"
VIProductVersion "1.2.0.0"
VIAddVersionKey /LANG=1033 "ProductName" "Fiber Voice"
VIAddVersionKey /LANG=1033 "FileDescription" "Fiber Voice Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "1.2.0"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Fiber Voice contributors"

!define MUI_ICON "AppIcon.ico"
!define MUI_UNICON "AppIcon.ico"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\FiberVoice.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open Fiber Voice"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; Ask for a clean quit instead of interrupting a call during an update/removal.
!macro CheckRunning
  System::Call 'kernel32::OpenMutexW(i 0x100000, i 0, w "Local\FiberVoice.Desktop") p.r0'
  ${If} $0 != 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_OK|MB_ICONINFORMATION "Quit Fiber Voice from its system tray menu, then run this setup again."
    Abort
  ${EndIf}
!macroend

Function .onInit
  SetShellVarContext current
  !insertmacro CheckRunning
FunctionEnd

Function un.onInit
  SetShellVarContext current
  !insertmacro CheckRunning
FunctionEnd

Section "Fiber Voice (required)" Main
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "build\app\FiberVoice.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortCut "$SMPROGRAMS\Fiber Voice.lnk" "$INSTDIR\FiberVoice.exe" "" "$INSTDIR\FiberVoice.exe" 0
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "Fiber Voice"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "1.2.0"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Fiber Voice"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" '"$INSTDIR\FiberVoice.exe",0'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Desktop shortcut" Desktop
  CreateShortCut "$DESKTOP\Fiber Voice.lnk" "$INSTDIR\FiberVoice.exe" "" "$INSTDIR\FiberVoice.exe" 0
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\Fiber Voice.lnk"
  Delete "$SMPROGRAMS\Fiber Voice.lnk"
  Delete "$INSTDIR\FiberVoice.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ; Keep credentials and call history in the separate application data folder.
SectionEnd
