example: example.c
	gcc -o $@ $^
	./$@
clean:
	rm -f example

web:
	uv run font-lig --font noto_sans_mono.ttf \
		--out noto_sans_mono_219.ttf \
		--sequence 219 \
		--family-name "Noto Sans Mono 219" \
		--logo 219.png \
		--scale 2.0
	python -m http.server 8080