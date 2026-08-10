# Prerequisite variables
SOURCEDIR := src
OUTPUTDIR := build

# Minecraft 26.2 loads org.lwjgl.util.spvc.Spvc during NativeLibrariesBootstrap.
# Amethyst replaces Mojang's LWJGL dependencies with its aggregate lwjgl.jar, so
# the SPVC Java bindings have to be included here as well. Keep this version in
# lock-step with the LWJGL version requested by Minecraft 26.2. Only the Java
# bindings are bundled; MetalUniversal supplies the iOS SPIRV-Cross C library.
LWJGL_RUNTIME_VERSION ?= 3.4.1
LWJGL_MAVEN_BASE ?= https://repo1.maven.org/maven2/org/lwjgl
LWJGL_SPVC_JAR := $(OUTPUTDIR)/deps/lwjgl-spvc-$(LWJGL_RUNTIME_VERSION).jar

SOURCES := $(shell find $(SOURCEDIR) -type f -name '*.java')
CLASSES := $(SOURCES:$(SOURCEDIR)/%.java=$(OUTPUTDIR)/%.class)

TARGETS := $(shell find $(SOURCEDIR) -mindepth 1 -maxdepth 1 -type d)
TARGETS := $(TARGETS:$(SOURCEDIR)/%=$(OUTPUTDIR)/%.jar)

# Add all jars to classpath and allow compiling with private API
JCFLAGS := -cp "$(subst $() $(),:,$(wildcard $(SOURCEDIR)/*/):$(wildcard libs/*/*.jar))" -XDignore.symbol.file

all: $(TARGETS)
	
# Sometimes the CI produces empty class file resulting in broken jar, so we check and abort here
check_empty_classes:
	@set -e
	@echo "Checking for empty class files"
	$(eval EMPTY_CLASSES := $(shell find $(OUTPUTDIR) -name '*.class' -size 0))
	@if [ -n "$(EMPTY_CLASSES)" ]; then \
		echo "Error: found empty class files in $@: $(EMPTY_CLASSES)"; \
		exit 1; \
	fi

# Rules for building java class
$(CLASSES): $(OUTPUTDIR)/%.class: $(SOURCEDIR)/%.java
	@set -e
	$(eval TARGET=$(word 2, $(subst /, ,$@)))
	@mkdir -p $(OUTPUTDIR)/classes
	@mkdir -p $(dir $@)
	@echo "Compiling $<"
	@$(BOOTJDK)/javac $(JCFLAGS) -d $(OUTPUTDIR)/classes $<
	@cp $(@:$(OUTPUTDIR)/$(TARGET)/%.class=$(OUTPUTDIR)/classes/%*.class) $(dir $@)/

$(OUTPUTDIR)/launcher.jar: $(CLASSES) check_empty_classes
	@set -e
	@echo "Creating $@"
	@$(BOOTJDK)/jar -cf $@ -C $(basename $@) .

$(LWJGL_SPVC_JAR):
	@set -e
	@echo "Fetching LWJGL SPVC Java bindings $(LWJGL_RUNTIME_VERSION)"
	@mkdir -p $(dir $@)
	@curl --fail --location --retry 3 --retry-delay 1 \
		-o "$@.tmp" \
		"$(LWJGL_MAVEN_BASE)/lwjgl-spvc/$(LWJGL_RUNTIME_VERSION)/lwjgl-spvc-$(LWJGL_RUNTIME_VERSION).jar"
	@mv "$@.tmp" "$@"

$(OUTPUTDIR)/lwjgl.jar: $(CLASSES) check_empty_classes $(LWJGL_SPVC_JAR)
	@set -e
	@echo "Creating $@"
	@rm -rf $(OUTPUTDIR)/lwjgl_lib
	@mkdir -p $(OUTPUTDIR)/lwjgl_lib
	@cd $(OUTPUTDIR)/lwjgl_lib && find ../../libs/lwjgl -name 'lwjgl*.jar' -exec $(BOOTJDK)/jar -xf {} \; && $(BOOTJDK)/jar -xf ../deps/lwjgl-spvc-$(LWJGL_RUNTIME_VERSION).jar && rm -rf META-INF
	@cp -R $(OUTPUTDIR)/lwjgl/* $(OUTPUTDIR)/lwjgl_lib/
	@test -f $(OUTPUTDIR)/lwjgl_lib/org/lwjgl/util/spvc/Spvc.class || (echo "Error: LWJGL SPVC bindings missing from aggregate lwjgl.jar"; exit 1)
	@$(BOOTJDK)/jar -cf $@ -C $(OUTPUTDIR)/lwjgl_lib .
	@$(BOOTJDK)/jar -tf $@ | grep -q '^org/lwjgl/util/spvc/Spvc.class$$' || (echo "Error: generated lwjgl.jar does not contain Spvc.class"; exit 1)

$(OUTPUTDIR)/patchjna_agent.jar: $(CLASSES)
	@set -e
	@mv $(basename $@)/com/sun/jna/Platform.class $(basename $@)/com/sun/jna/Platform.class.patch
	@echo "Creating $@"
	@$(BOOTJDK)/jar -cfm $@ $(SOURCEDIR)/patchjna_agent.txt -C $(basename $@) .

clean:
	rm -rf $(OUTPUTDIR)

.SUFFIXES: .java
.PHONY: all clean
