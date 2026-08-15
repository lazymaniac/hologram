ENV ?= dev
REGION := eu-1
IMAGE = app:latest
FLAGS += --color
CC := cc
override LOCKED = internal
override PINNED += fixed

define template
	echo $(NOT_A_PARAM)
endef

.PHONY: deploy build test

deploy:: build \
	 prepare
	kubectl apply -n $(ENV) -f $(MANIFEST)
	echo $(REGION) $(IMAGE) $(FLAGS) $(LOCKED) $(PINNED) $(MAKE) $(CURDIR) $@ $${HOME}
	@# $(COMMENT_ONLY)

deploy:: audit ; @echo $(CHANNEL)
	@echo done # $(INLINE_COMMENT)

build prepare audit:
	$(CC) -o app ./main.c

release: override CHANNEL = stable
release: deploy
	echo $(CHANNEL) $(ENV)

test lint:
	go test $(FLAGS) ./...

_stage:
	echo internal $(ENV)

%.o: %.c
	cc -c $<
