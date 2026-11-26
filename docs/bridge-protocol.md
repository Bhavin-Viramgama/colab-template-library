# Bridge Protocol

Documentation for the communication protocol between the Colab Template Launcher Android app and the Colab runtime.

## Overview

The Android app and Colab runtime communicate through JSON status files stored on Google Drive. This decoupled approach works reliably across the network boundary between the user's phone and the Colab VM.

## Architecture

```
Android App <---> Google Drive <---> Colab Runtime
                   (status files)
```

## Task Lifecycle

1. **App creates task**: Writes task parameters to Drive
2. **User runs notebook**: Notebook picks up pending task
3. **Runtime executes**: Module runs and updates status
4. **App polls status**: Reads status file periodically
5. **Task completes**: Final status written to Drive

## Directory Structure

```
/MyDrive/.colab-tasks/
├── runtime_status.json              # Runtime information
├── {task_id}/
│   ├── status.json                  # Task status (read by app)
│   └── control.json                 # Control commands (written by app)
```

## Task Status Format

```json
{
  "taskId": "direct-download_a1b2c3d4_1700000000",
  "templateId": "direct-download",
  "templateName": "Direct Download",
  "status": "downloading",
  "progress": 62,
  "progressMessage": "Downloading file...",
  "filename": "video.mp4",
  "totalBytes": 4200000000,
  "completedBytes": 2600000000,
  "speedBytesPerSecond": 18400000,
  "etaSeconds": 84,
  "inputs": {
    "url": "https://example.com/video.mp4",
    "destination": "/MyDrive/Downloads"
  },
  "result": null,
  "error": null,
  "createdAt": 1700000000.0,
  "updatedAt": 1700000085.0
}
```

## Status Values

| Status | Description |
|--------|-------------|
| `pending` | Task created, waiting for execution |
| `downloading` | Download in progress |
| `paused` | Task paused by user |
| `completed` | Task finished successfully |
| `failed` | Task failed with error |
| `cancelled` | Task cancelled by user |

## Control Commands

The Android app can send control commands by writing to `control.json`:

```json
{
  "command": "cancel",
  "timestamp": 1700000100.0
}
```

Supported commands: `pause`, `resume`, `cancel`

## Runtime Status Format

```json
{
  "connectionStatus": "connected",
  "runtimeType": "GPU",
  "ramUsedMb": 2048,
  "ramTotalMb": 12288,
  "storageUsedMb": 15000,
  "storageTotalMb": 78000,
  "gpuName": "Tesla T4",
  "gpuMemoryMb": 15360,
  "sessionStartTime": 1700000000.0,
  "timestamp": 1700000100.0
}
```

## Polling Strategy

The Android app polls status files at the following intervals:
- Active task: Every 3 seconds
- Idle monitoring: Every 30 seconds
- Runtime status: Every 60 seconds

## Security Considerations

- Task files are stored in the user's private Google Drive
- No authentication tokens are stored in status files
- URLs are validated before execution
- No arbitrary code execution from status files
- Control commands are limited to predefined operations
