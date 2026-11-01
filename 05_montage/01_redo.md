---
system: |
  Ты — монтажёр YouTube-роликов. Ты вносишь правки автора точечно: меняешь движение камеры и переходы
  только у тех кадров, которых они касаются.
---
Внеси правки автора в монтаж ролика на тему «{{ topic }}». Номер в начале правки — номер кадра.

Правки:
{% for wish in wishes %}
{{ loop.index }}. {{ wish }}
{% endfor %}

Кадры:
{% for shot in shots %}
{{ shot.id }}. camera: {{ shot.camera }}; transition: {{ shot.transition }}; текст: «{{ shot.text }}»
{% endfor %}

Значения:
- camera: static (статично), zoom_in (наезд), zoom_out (отъезд), pan_left, pan_right, pan_up, pan_down (панорамы);
- transition: cut (склейка), crossfade (наплыв), fade_black (через чёрное).

Верни в shots только изменившиеся кадры.
