example: example.c
	gcc -o $@ $^
	./$@
clean:
	rm -f example

web:
	uv run font-lig --font noto_sans_mono.ttf \
		--out noto_sans_mono_cool.ttf \
		--sequence cool \
		--family-name "Noto Sans Mono Cool" \
		--logo cool.png \
		--scale 2.0
	python -m http.server 8080