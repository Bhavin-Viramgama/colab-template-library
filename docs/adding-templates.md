# Adding New Templates

This guide explains how to add new templates to the Colab Template Library.

## Steps

### 1. Create the template directory

```
templates/your-template-id/
├── template.json       # Required: Template metadata
├── module.py           # Required for Colab templates: Execution module
└── notebook.ipynb      # Required for Colab templates: Colab notebook
```

### 2. Define template.json

```json
{
  "id": "your-template-id",
  "name": "Your Template Name",
  "description": "What this template does",
  "version": "1.0.0",
  "category": "Category",
  "icon": "icon_name",
  "executionType": "colab | local",
  "runtimeRequired": true,
  "inputs": [
    {
      "id": "input_id",
      "label": "Input Label",
      "type": "text | url | number | select",
      "required": true,
      "placeholder": "Placeholder text",
      "description": "What this input is for",
      "default": "optional default value",
      "validation": {
        "pattern": "regex pattern",
        "message": "Validation error message"
      }
    }
  ],
  "outputs": [
    {
      "id": "output_id",
      "label": "Output Label",
      "type": "text | number",
      "description": "What this output represents"
    }
  ]
}
```

### 3. Add to manifest.json

Add your template entry to the root `manifest.json`:

```json
{
  "id": "your-template-id",
  "name": "Your Template Name",
  "description": "What this template does",
  "category": "Category",
  "icon": "icon_name",
  "executionType": "colab",
  "runtimeRequired": true,
  "metadataPath": "templates/your-template-id/template.json",
  "version": "1.0.0",
  "author": "Your Name"
}
```

### 4. Implement the execution module (Colab templates)

Create a Python module that:
- Accepts validated inputs
- Reports progress via task status files
- Handles errors gracefully
- Returns structured results

### 5. Create the notebook (Colab templates)

Create a Jupyter notebook that:
- Clones this repository
- Installs dependencies
- Mounts Google Drive
- Executes the template module
- Reports status

### 6. Test

1. Open the notebook in Google Colab
2. Run all cells
3. Verify the template works correctly
4. Push changes to this repository

The Android app will automatically detect the new template on its next manifest refresh.

## Template Types

### Local Templates
- Run directly on the Android device
- No runtime or Google Drive required
- Execution logic is in the Android app itself
- Only `template.json` is needed in this repository

### Colab Templates
- Run on Google Colab runtime
- May require Google Drive access
- Include Python module and notebook
- Communicate via status files on Drive

## Input Types

| Type | Description | Validation |
|------|-------------|------------|
| `text` | Free-form text input | Optional regex pattern |
| `url` | URL input | HTTP/HTTPS validation |
| `number` | Numeric input | Min/max range |
| `select` | Dropdown selection | List of options |

## Icon Names

Use Material Icons names. The Android app maps these to `androidx.compose.material.icons.Icons`:
`download`, `calculator`, `image`, `video_file`, `audio_file`, `folder`, `cloud`, `code`, `analytics`, `build`
