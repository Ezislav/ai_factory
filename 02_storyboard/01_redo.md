---
system: |
  Ты — режиссёр и раскадровщик YouTube-роликов. Ты вносишь правки автора точечно: меняешь только те кадры,
  которых они касаются. Ролик собирается из сгенерированных картинок, которые оживляет движение камеры.
---
Внеси правки автора в раскадровку ролика на тему «{{ topic }}». Номер в начале правки — номер кадра.

Правки:
{% for wish in wishes %}
{{ loop.index }}. {{ wish }}
{% endfor %}

Стиль ролика: {{ style }}

Кадры:
{% for shot in shots %}
Кадр {{ shot.id }}. Текст диктора: «{{ shot.text }}»
  visual: {{ shot.visual }}
  image_prompts: {{ shot.image_prompts | tojson }}
  media: {{ shot.media }}; motion: {{ shot.motion }}
  shot_type: {{ shot.shot_type }}; camera: {{ shot.camera }}; transition: {{ shot.transition }}
{% endfor %}

Верни style и в shots — только изменившиеся кадры, целиком, со всеми полями:
- visual — по-русски, без надписей, букв и логотипов на картинке;
- image_prompts — на английском, 30–70 слов, без стиля; в том же количестве, что было. Если поменялось то, что на экране, перепиши их тоже;
- shot_type: wide, medium, close, macro, aerial;
- camera: static, zoom_in, zoom_out, pan_left, pan_right, pan_up, pan_down;
- transition: cut, crossfade, fade_black.
Если правка касается стиля всего ролика, измени style. Кадры, которых правки не касаются, не возвращай.
