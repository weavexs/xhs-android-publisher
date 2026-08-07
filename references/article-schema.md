# Article input contract

Store article directories below the configured `article_root`:

```text
article_root/
└── 001_title/
    ├── 发布内容.json
    ├── 正文.txt
    └── output/
        ├── 01-cover.png
        └── 02-page.png
```

`发布内容.json`:

```json
{
  "id": "001",
  "status": "ready",
  "content_rules_version": 2,
  "title": "标题不超过20字",
  "body_file": "正文.txt",
  "images": [
    "output/01-cover.png",
    "output/02-page.png"
  ],
  "expected_image_size": [1125, 1500],
  "tags": ["账号话题"]
}
```

Rules:

- Match `id` to the directory prefix.
- Publish only `status: ready`.
- Keep title at 2–20 characters and body at 1–1000 characters.
- Use 1–6 valid PNG images in intended order.
- Put the cover first and use zero-padded names.
- Include the configured `required_tag` both in `tags` and as `#话题` in the body when
  `content_rules_version` is 2.
- Treat `published` as a duplicate blocker.
