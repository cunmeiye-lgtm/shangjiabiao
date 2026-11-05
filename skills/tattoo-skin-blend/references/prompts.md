# 编辑提示词

每张照片是独立编辑目标。保留每张实际使用的最终提示词。以下模板中的设计描述替换为**从本图观察到的特征**，不把八卦、蝴蝶或花朵添加到不包含这些元素的图片。

## 正常显色：共同约束

```text
Use case: precise-object-edit.
Input image 1 is the edit target, an ordinary commercial tattoo product photograph.
Make a minimal localized edit ONLY to the existing [observed design] tattoo on [observed body area], so its pigment is naturally integrated with the underlying skin like flat healed tattoo ink.
Preserve the exact original design, all characters and symbol strokes, fine lines and tiny ornaments, proportions, placement, size, orientation and coverage. Do not invent, remove, mirror or simplify any part of the design.
Match ink density gently to local skin lighting and body curvature. Keep natural pores, fine skin texture, micro creases and any existing fine body hair visible through or over the pigment. Subtly soften only unnaturally pasted-on edges while keeping the design clear and complete.
Keep the tattoo entirely flat: no raised ink, cast shadows, sticker border, plastic film, extra shine, redness or swelling.
Preserve the original person's identity, skin tone, anatomy, veins, arm and hand shape, pose, hair, clothing and coverage, jewelry, accessories, crop, composition, background and lighting as closely as possible. No beautification, no added objects, text or watermark. Return one complete photograph with the original aspect ratio, no collage.
```

合并适用的颜色段，再送给AI工具；不要单独使用颜色段。

### 黑色图案

```text
Keep a natural deep charcoal black with subtle skin undertones rather than a uniformly pure digital black. Retain strong legibility, complete calligraphy strokes and crisp fine details. Do not heavily fade, blur, recolor or erase the tattoo.
```

识别到太极/八卦时追加：

```text
Preserve exactly the original yin-yang shape and dots, all eight trigram line arrangements, octagonal outlines and Chinese calligraphy. Match the reference strokes directly; do not regenerate text from a guessed transcription.
```

### 彩色/水彩图案

```text
Preserve the original color palette, color balance and shading, including airy watercolor gradients and intentional negative space. Keep every observed flower, petal, butterfly wing vein, decorative curl, tiny dot or star in its original form. Do not convert to black ink, heavily darken, desaturate, wash out, erase parts or make the tattoo nearly transparent.
```

### 多处纹身

按实际画面定位，例如：

```text
Edit only the central vertical Bagua and calligraphy tattoo on the lower forearm. Keep the separate existing writing tattoo on the other crossing forearm unchanged.
```

不能仅凭此例在其他照片中猜测“下方小臂”或“另一只手臂”。

## 明确要求刚揭下/近透明时

此段仅在当前请求明确要求时，替换正常显色模板的“healed tattoo ink”及不透明度相关要求，不和“不得接近透明”并用。外观以用户本款参考或当前确认要求为准，不统一断言所有“草本”产品刚揭下的化学颜色与显色时间。

```text
Change ONLY the state of the existing tattoo to the just-peeled appearance shown in the user's reference: [current requested tint and visibility]. Keep its exact design footprint, position, size and orientation. Preserve natural skin texture and lighting. No opaque black ink, invented outline, adhesive border, plastic film, redness or swelling. Keep the rest of the photograph as closely unchanged as possible.
```

用户要求颜色再淡时，只降低其指定痕迹的可见度；若进一步明确要求完全去掉痕迹，按独立的去除编辑处理，不能将其存为正常融合的结果或沿用到下一批。
