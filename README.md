# Colab Template Library

Public template library for the **Colab Template Launcher** Android application.

## Overview

This repository contains templates that can be browsed and executed from the Colab Template Launcher app. Templates are defined as metadata + execution modules, allowing new templates to be added without updating the Android app.

## Structure

```
├── manifest.json                  # Template manifest (fetched by the app)
├── runtime/                       # Shared runtime utilities
│   ├── colab_bridge.py           # Runtime info and bridge protocol
│   ├── task_manager.py           # Task lifecycle management
│   └── requirements.txt          # Python dependencies
├── templates/
│   ├── direct-download/          # Direct Download template
│   │   ├── template.json         # Template metadata and input schema
│   │   ├── downloader.py         # aria2-based download module
│   │   └── notebook.ipynb        # Colab notebook
│   └── calculator/               # Calculator template
│       └── template.json         # Template metadata (runs locally)
└── docs/                         # Documentation
    ├── adding-templates.md       # Guide for adding new templates
    └── bridge-protocol.md        # Bridge protocol documentation
```

## Templates

| Template | Type | Runtime Required | Description |
|----------|------|-----------------|-------------|
| Direct Download | Colab | Yes | Download files to Google Drive using aria2 |
| AI Chat | Colab | Yes | ChatGPT-style conversational AI running on Colab |

## Adding New Templates

See [docs/adding-templates.md](docs/adding-templates.md) for detailed instructions.

## Bridge Protocol

See [docs/bridge-protocol.md](docs/bridge-protocol.md) for details on how the Android app communicates with Colab.

## License

MIT License - see [LICENSE](LICENSE) for details.
