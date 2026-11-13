#define AppName "Layer Recorder"
[Setup]
AppId={{1BAA861B-B01E-4458-89EC-F93E9B68D6B1}
AppName={#AppName}
AppVersion=0.8.0
AppPublisher=Layer Recorder
AppPublisherURL=https://github.com/miruas/layer-recorder-windows
DefaultDirName={localappdata}\Programs\Layer Recorder
DefaultGroupName=Layer Recorder
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=Local\LayerRecorder.Instance
CloseApplications=no
UninstallDisplayIcon={app}\katman\assets\layer.ico
SetupIconFile={#Stage}\katman\assets\layer.ico
Compression=lzma2/fast
SolidCompression=yes
ArchiveExtraction=full
OutputDir={#OutDir}
OutputBaseFilename=Layer-Setup-x64
ChangesAssociations=yes
WizardStyle=modern

[Tasks]
Name: "startup"; Description: "Start Layer in the notification area when I sign in"; Flags: unchecked
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "projects"; Description: "Add Layer Studio to Open with for .katman projects"; Flags: unchecked

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__,*.pyc,*.pyo"
Source: "{tmp}\ffmpeg.zip"; DestDir: "{app}\media-tools"; Flags: external extractarchive ignoreversion recursesubdirs; Excludes: "*.html,*.ffpreset"; Check: NeedsMedia

[Icons]
Name: "{group}\Layer Recorder"; Filename: "{app}\Layer.exe"; WorkingDir: "{app}"; IconFilename: "{app}\katman\assets\layer.ico"
Name: "{group}\Layer Studio"; Filename: "{app}\Layer.exe"; Parameters: "editor"; WorkingDir: "{app}"; IconFilename: "{app}\katman\assets\layer.ico"
Name: "{userdesktop}\Layer Recorder"; Filename: "{app}\Layer.exe"; WorkingDir: "{app}"; IconFilename: "{app}\katman\assets\layer.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LayerRecorder"; ValueData: """{app}\Layer.exe"" --background"; Flags: uninsdeletevalue; Tasks: startup
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "LayerRecorder"; Flags: deletevalue; Tasks: not startup
Root: HKCU; Subkey: "Software\Classes\.katman\OpenWithProgids"; ValueType: string; ValueName: "LayerRecorder.Project"; ValueData: ""; Flags: uninsdeletevalue; Tasks: projects
Root: HKCU; Subkey: "Software\Classes\LayerRecorder.Project"; ValueType: string; ValueData: "Layer project"; Flags: uninsdeletekey; Tasks: projects
Root: HKCU; Subkey: "Software\Classes\LayerRecorder.Project\DefaultIcon"; ValueType: string; ValueData: "{app}\katman\assets\layer.ico"; Tasks: projects
Root: HKCU; Subkey: "Software\Classes\LayerRecorder.Project\shell\open\command"; ValueType: string; ValueData: """{app}\Layer.exe"" ""%1"""; Tasks: projects

[Run]
Filename: "{app}\Layer.exe"; Description: "Open Layer Recorder"; Flags: nowait postinstall skipifsilent

[Code]
var
  DownloadPage: TDownloadWizardPage;
  InstallMedia: Boolean;

procedure InitializeWizard();
begin
  DownloadPage := CreateDownloadPage('Download media tools', 'Layer needs FFmpeg for capture and editing.', nil);
end;

function NeedsMedia(): Boolean;
begin
  Result := InstallMedia;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  InstallMedia := not FileExists(ExpandConstant('{app}\media-tools\ffmpeg-9.0.2-full_build\bin\ffmpeg.exe')) or
                  not FileExists(ExpandConstant('{app}\media-tools\ffmpeg-9.0.2-full_build\bin\ffprobe.exe'));
  if not InstallMedia then exit;
  DownloadPage.Clear;
  DownloadPage.Add('https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-full_build.zip',
    'ffmpeg.zip', '759d0a9831c436a0eb331ad36f236c06cb04aaa0005da46571f0e9d3d9206f6b');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      Result := GetExceptionMessage;
    end;
  finally
    DownloadPage.Hide;
  end;
end;
