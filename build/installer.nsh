!define /ifndef KRYPT_BACKEND_EXE "krypt-polybot-backend.exe"

!macro kryptStopBackendRelays
  Push $0
  Push $1
  ReadEnvStr $1 USERNAME
  ${If} $1 != ""
    DetailPrint `Closing Krypt PolyBot connections used by AI clients...`
    nsExec::Exec `"$SYSDIR\taskkill.exe" /F /IM "${KRYPT_BACKEND_EXE}" /FI "USERNAME eq $1"`
    Pop $0
    Sleep 1500
    nsExec::Exec `"$SYSDIR\taskkill.exe" /F /IM "${KRYPT_BACKEND_EXE}" /FI "USERNAME eq $1"`
    Pop $0
    Sleep 500
  ${EndIf}
  Pop $1
  Pop $0
!macroend

!include "getProcessInfo.nsh"
Var pid

!macro customCheckAppRunning
  !ifmacrondef _CHECK_APP_RUNNING
    !error "electron-builder no longer defines _CHECK_APP_RUNNING; re-check build/installer.nsh against templates/nsis/include/allowOnlyOneInstallerInstance.nsh"
  !endif
  !insertmacro _CHECK_APP_RUNNING
  !insertmacro kryptStopBackendRelays
!macroend

!macro customInit
  ${If} ${UAC_IsInnerInstance}
    !insertmacro kryptStopBackendRelays
  ${EndIf}
!macroend

!macro customUnInstall
!macroend
