# 研读随笔写作约定

> 该目录作为历史文章的可重放迁移源；运行时权威内容位于用户数据库。新的用户阅读感想由网站个人工作区或
> `masterstock agent reading` 管理：保存和提交始终私有，只有站长明确共享的冻结版本
> 才进入站长动态。

每篇文章使用独立目录，目录名就是 URL slug：

```text
reading/
  article-slug/
    index.md
    images/
      example.jpg
```

`index.md` 使用 TOML front matter 和 Markdown 正文：

```markdown
+++
title = "归档页与翻页使用的短标题"
heading = "文章页使用的完整标题"
deck = "文章标题下方的导读。"
summary = "归档页和站长动态使用的摘要。"
eyebrow = "交易研究 · 主题"
subtitle = "网站页头的副标题"
published_on = "2026-08-22"
tags = ["趋势交易", "风险管理"]
status = "draft"
+++

正文使用 Markdown。
```

- `status = "draft"` 不会出现在网站；改为 `published` 后随下次部署发布。
- 图片使用相对路径，例如 `![说明](images/example.jpg)`。文章加载时会校验文件存在且没有越出文章目录。
- 表格、列表、引用和代码块使用标准 Markdown。
- 需要现有特殊样式时，可使用仓库受控的 HTML，例如 `reading-disclaimer`、`reading-cycle` 和 `reading-figure`。
- 文章仍是登录后内容，资源图片也通过同一权限路由访问。
