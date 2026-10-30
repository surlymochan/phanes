; electron-builder includes this file in the NSIS installer. Its `protocols` setting registers the
; openbot:// scheme only on macOS and Linux, so the installer writes the Windows key itself.
; The application writes the same key at each start (`src/main/index.ts`).

; A chosen folder such as D:\OpenBot inherits Modify rights for "Authenticated Users" from the drive
; root, so another account could replace OpenBot.exe. Give access only to this user, SYSTEM and
; Administrators, as %LOCALAPPDATA% does, and reset the files to inherit it. This runs before the
; files are copied at each install and update, because an update deletes the folder. The installer
; makes the folder, so this user owns it and icacls can change it. On a drive with no ACLs, such as
; exFAT, icacls fails and the install continues.
!macro openBotRestrictInstallDirectory
  Push $0
  Push $1
  ReadEnvStr $0 USERDOMAIN
  ReadEnvStr $1 USERNAME
  nsExec::ExecToLog '"$SYSDIR\icacls.exe" "$INSTDIR" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "$0\$1:(OI)(CI)F" /Q'
  Pop $0
  ${if} $0 == 0
    nsExec::ExecToLog '"$SYSDIR\icacls.exe" "$INSTDIR\*" /reset /T /C /Q'
    Pop $0
  ${endIf}
  Pop $1
  Pop $0
!macroend

; This replaces the check after the earlier version is removed, so it keeps that check first.
!macro customUnInstallCheck
  ${if} ${Errors}
    DetailPrint `Uninstall was not successful. Not able to launch uninstaller!`
  ${elseIf} $R0 != 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0"
    DetailPrint `Uninstall was not successful. Uninstaller error code: $R0.`
    SetErrorLevel 2
    Quit
  ${endIf}
  CreateDirectory "$INSTDIR"
  !insertmacro openBotRestrictInstallDirectory
!macroend

!macro customInstall
  WriteRegStr SHCTX "Software\Classes\openbot" "" "URL:OpenBot invitations"
  WriteRegStr SHCTX "Software\Classes\openbot" "URL Protocol" ""
  WriteRegStr SHCTX "Software\Classes\openbot\DefaultIcon" "" "$appExe,0"
  WriteRegStr SHCTX "Software\Classes\openbot\shell\open\command" "" '"$appExe" "%1"'
!macroend

; An update runs the old uninstaller first. Keep the key then, so that links work during the update.
; Delete the key only when it still opens this copy. Another copy can own it now.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    Push $0
    ReadRegStr $0 SHCTX "Software\Classes\openbot\shell\open\command" ""
    ${if} $0 == '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" "%1"'
      DeleteRegKey SHCTX "Software\Classes\openbot"
    ${endIf}
    Pop $0
  ${endIf}
!macroend

; OpenBot installs for the current user only. The application writes the openbot:// key in HKCU at
; each start, and an update finds only one installation. This skips the "All users" page.
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
!macroend

; The uninstaller and each update delete the whole installation folder. electron-builder adds an
; OpenBot subfolder only when the chosen path does not contain "OpenBot", so a choice such as
; %USERPROFILE%\OpenBot (the agent workspaces) or %APPDATA%\OpenBot (the user data) would be
; deleted. Another account can also make a folder first and keep its rights on it. Use only a folder
; that does not exist or that holds this user's earlier installation. Until then, add an OpenBot
; subfolder. The page shows nothing; Abort skips it.
!macro customPageAfterChangeDir
  !include FileFunc.nsh

  Page custom openBotSafeInstallDirectory

  Function openBotSafeInstallDirectory
    Push $0
    Push $1
    ReadRegStr $1 HKCU "${INSTALL_REGISTRY_KEY}" InstallLocation
    StrCpy $0 $INSTDIR 1 -1
    ${if} $0 == "\"
      StrCpy $INSTDIR $INSTDIR -1
    ${endIf}
    ${StrContains} $0 "${APP_FILENAME}" $INSTDIR
    ${if} $0 == ""
      StrCpy $INSTDIR "$INSTDIR\${APP_FILENAME}"
    ${endIf}
    ${do}
      ${if} $INSTDIR == $1
        ${exitDo}
      ${endIf}
      ${DirState} "$INSTDIR" $0
      ${if} $0 == -1
        ${exitDo}
      ${endIf}
      StrCpy $INSTDIR "$INSTDIR\${APP_FILENAME}"
    ${loop}
    Pop $1
    Pop $0
    Abort
  FunctionEnd
!macroend
