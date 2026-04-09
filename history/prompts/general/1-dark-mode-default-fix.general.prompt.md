---
id: 1
title: Dark Mode Default Fix
stage: general
date: 2026-04-09
surface: agent
model: claude-sonnet-4-6
feature: none
branch: main
user: Janna
command: chat
labels: ["dark-mode", "theme", "ui", "flash-fix"]
links:
  spec: null
  ticket: null
  adr: null
  pr: null
files:
  - templates/index.html
  - templates/login.html
tests: []
---

## Prompt

when we open the app make the dark mode come first

## Response

Moved the IIFE theme-setting script from after `<body>` into `<head>` (before fonts/styles) in both `index.html` and `login.html`. This ensures the script runs synchronously before browser rendering, setting `data-theme="dark"` as default (falling back to localStorage preference). Eliminates flash of light mode on first load.

Key change in both files (lines 7-12):
```html
<script>
    (function() {
        const saved = localStorage.getItem('theme') || 'dark';
        document.documentElement.setAttribute('data-theme', saved);
    })();
</script>
```

## Outcome

- Dark mode is now the default on first app open
- Theme preference persists via localStorage on subsequent visits
- No flash of wrong theme on load
