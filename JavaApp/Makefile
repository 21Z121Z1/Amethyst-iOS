# Prerequisite variables
SOURCEDIR := src
OUTPUTDIR := build

# Minecraft 26.2 requires LWJGL 3.4.1-era Java APIs. The iOS launcher cannot
# safely mix those Java bindings with its older iOS-native LWJGL build, so this
# integration branch consumes one coherent AngelAura iOS LWJGL runtime: the
# module directory produced as bin/RELEASE and the matching natives in bin/out.
# A helper script in scripts/ builds the pinned runtime reproducibly.
LWJGL_RUNTIME_VERSION ?= 3.4.1
LWJGL_MODULES_DIR ?=
LWJGL_IOS_NATIVES_DIR ?=

# Optional MetalUniversal integration. Point this at the directory produced by
# `./gradlew buildIOSNative buildIOSSpvc`, for example:
#   METALLUM_NATIVE_DIR=/abs/path/MetalUniversal/src/main/resources/natives/ios
# The root Amethyst Makefile already copies Natives/build/*.dylib into the app's
# Frameworks directory, so staging here keeps the normal packaging/signing path.
METALLUM_NATIVE_DIR ?=
METALLUM_STAGE_DIR := ../Natives/build

SOURCES := $(shell find $(SOURCEDIR) -type f -name '*.java')
CLASSES := $(SOURCES:$(SOURCEDIR)/%.java=$(OUTPUTDIR)/%.class)

TARGETS := $(shell find $(SOURCEDIR) -mindepth 1 -maxdepth 1 -type d)
TARGETS := $(TARGETS:$(SOURCEDIR)/%=$(OUTPUTDIR)/%.jar)

# Keep launcher/caciocavallo dependencies, but never put the repository's old
# LWJGL jars next to the 3.4.1 modules on javac's class path. Duplicate versions
# are exactly what caused the Spvc/GLFW linkage failures on Minecraft 26.x.
NON_LWJGL_LIB_JARS := $(filter-out $(wildcard libs/lwjgl/*.jar),$(wildcard libs/*/*.jar))
LWJGL_MODULE_JARS := $(shell if [ -n "$(strip $(LWJGL_MODULES_DIR))" ] && [ -d "$(LWJGL_MODULES_DIR)" ]; then find "$(LWJGL_MODULES_DIR)" -type f -name 'lwjgl*.jar' -print | sort; fi)
JAVA_CLASSPATH_JARS := $(NON_LWJGL_LIB_JARS) $(LWJGL_MODULE_JARS)

# Add all selected jars to classpath and allow compiling with private API.
JCFLAGS := -cp "$(subst $() $(),:,$(wildcard $(SOURCEDIR)/*/):$(JAVA_CLASSPATH_JARS))" -XDignore.symbol.file

all: require_lwjgl_modules $(TARGETS) stage_lwjgl_natives stage_metallum_natives

require_lwjgl_modules:
	@set -e
	@if [ -z "$(strip $(LWJGL_MODULES_DIR))" ]; then \
		echo "Error: LWJGL_MODULES_DIR is required on feature/metallum-mc26-lwjgl-compat."; \
		echo "Build AngelAuraMC/lwjgl3 wip/rebase_3.4.1 (pinned by scripts/build_metallum_runtime.sh) and point this variable at bin/RELEASE."; \
		exit 1; \
	fi
	@test -d "$(LWJGL_MODULES_DIR)" || (echo "Error: LWJGL_MODULES_DIR is not a directory: $(LWJGL_MODULES_DIR)"; exit 1)
	@test -n "$(shell find "$(LWJGL_MODULES_DIR)" -type f -name 'lwjgl.jar' -print -quit 2>/dev/null)" || (echo "Error: coherent LWJGL core jar missing under $(LWJGL_MODULES_DIR)"; exit 1)
	@test -n "$(shell find "$(LWJGL_MODULES_DIR)" -type f -name 'lwjgl-glfw.jar' -print -quit 2>/dev/null)" || (echo "Error: coherent LWJGL GLFW jar missing under $(LWJGL_MODULES_DIR)"; exit 1)
	@test -n "$(shell find "$(LWJGL_MODULES_DIR)" -type f -name 'lwjgl-spvc.jar' -print -quit 2>/dev/null)" || (echo "Error: coherent LWJGL SPVC jar missing under $(LWJGL_MODULES_DIR)"; exit 1)
	@echo "Using coherent LWJGL $(LWJGL_RUNTIME_VERSION) modules from $(LWJGL_MODULES_DIR)"

stage_lwjgl_natives:
	@set -e
	@if [ -z "$(strip $(LWJGL_IOS_NATIVES_DIR))" ]; then \
		echo "Error: LWJGL_IOS_NATIVES_DIR is required for an Amethyst iOS package."; \
		echo "Point it at bin/out from the same AngelAura LWJGL build as LWJGL_MODULES_DIR."; \
		exit 1; \
	fi
	@test -d "$(LWJGL_IOS_NATIVES_DIR)" || (echo "Error: LWJGL_IOS_NATIVES_DIR is not a directory: $(LWJGL_IOS_NATIVES_DIR)"; exit 1)
	@mkdir -p "$(METALLUM_STAGE_DIR)"
	@count=0; \
	for source in "$(LWJGL_IOS_NATIVES_DIR)"/*.dylib; do \
		[ -f "$$source" ] || continue; \
		count=$$((count + 1)); \
		target="$(METALLUM_STAGE_DIR)/$$(basename "$$source")"; \
		cp "$$source" "$$target"; \
		file "$$target" | grep -q 'Mach-O 64-bit arm64' || (echo "Error: $$target is not an arm64 Mach-O"; exit 1); \
		if command -v vtool >/dev/null 2>&1; then \
			vtool -show-build "$$target" | grep -qi 'platform IOS' || (echo "Error: $$target is not built for iOS"; exit 1); \
		fi; \
		if command -v ldid >/dev/null 2>&1; then ldid -S -M "$$target"; fi; \
	done; \
	[ "$$count" -gt 0 ] || (echo "Error: no iOS LWJGL dylibs found in $(LWJGL_IOS_NATIVES_DIR)"; exit 1); \
	echo "Staged $$count coherent LWJGL iOS natives into Amethyst Frameworks input"

stage_metallum_natives:
	@set -e
	@if [ -n "$(strip $(METALLUM_NATIVE_DIR))" ]; then \
		echo "Staging MetalUniversal iOS natives from $(METALLUM_NATIVE_DIR)"; \
		test -f "$(METALLUM_NATIVE_DIR)/libmetallum.dylib" || (echo "Error: missing libmetallum.dylib; run MetalUniversal buildIOSNative first"; exit 1); \
		test -f "$(METALLUM_NATIVE_DIR)/libspvc.dylib" || (echo "Error: missing libspvc.dylib; run MetalUniversal buildIOSSpvc first"; exit 1); \
		mkdir -p "$(METALLUM_STAGE_DIR)"; \
		cp "$(METALLUM_NATIVE_DIR)/libmetallum.dylib" "$(METALLUM_STAGE_DIR)/libmetallum.dylib"; \
		cp "$(METALLUM_NATIVE_DIR)/libspvc.dylib" "$(METALLUM_STAGE_DIR)/libspvc_metallum.dylib"; \
		file "$(METALLUM_STAGE_DIR)/libmetallum.dylib" | grep -q 'Mach-O 64-bit arm64' || (echo "Error: libmetallum.dylib is not an arm64 Mach-O"; exit 1); \
		file "$(METALLUM_STAGE_DIR)/libspvc_metallum.dylib" | grep -q 'Mach-O 64-bit arm64' || (echo "Error: libspvc_metallum.dylib is not an arm64 Mach-O"; exit 1); \
		if command -v vtool >/dev/null 2>&1; then \
			vtool -show-build "$(METALLUM_STAGE_DIR)/libmetallum.dylib" | grep -qi 'platform IOS' || (echo "Error: libmetallum.dylib is not built for iOS"; exit 1); \
			vtool -show-build "$(METALLUM_STAGE_DIR)/libspvc_metallum.dylib" | grep -qi 'platform IOS' || (echo "Error: libspvc_metallum.dylib is not built for iOS"; exit 1); \
		fi; \
		if command -v ldid >/dev/null 2>&1; then \
			ldid -S -M "$(METALLUM_STAGE_DIR)/libmetallum.dylib"; \
			ldid -S -M "$(METALLUM_STAGE_DIR)/libspvc_metallum.dylib"; \
		fi; \
		echo "MetalUniversal natives staged for Amethyst Frameworks"; \
	else \
		echo "MetalUniversal native staging disabled (METALLUM_NATIVE_DIR not set)"; \
	fi

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
$(CLASSES): require_lwjgl_modules
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

$(OUTPUTDIR)/lwjgl.jar: require_lwjgl_modules $(CLASSES) check_empty_classes
	@set -e
	@echo "Creating coherent aggregate LWJGL $(LWJGL_RUNTIME_VERSION) jar"
	@rm -rf $(OUTPUTDIR)/lwjgl_lib
	@mkdir -p $(OUTPUTDIR)/lwjgl_lib
	@set -e; \
	for module in $$(find "$(LWJGL_MODULES_DIR)" -type f -name 'lwjgl*.jar' -print | sort); do \
		(cd $(OUTPUTDIR)/lwjgl_lib && $(BOOTJDK)/jar -xf "$$OLDPWD/$$module"); \
	done
	@rm -rf $(OUTPUTDIR)/lwjgl_lib/META-INF
	@cp -R $(OUTPUTDIR)/lwjgl/* $(OUTPUTDIR)/lwjgl_lib/
	@test -f $(OUTPUTDIR)/lwjgl_lib/org/lwjgl/util/spvc/Spvc.class || (echo "Error: LWJGL SPVC bindings missing from aggregate lwjgl.jar"; exit 1)
	@test -f $(OUTPUTDIR)/lwjgl_lib/org/lwjgl/glfw/GLFWPreeditCallback.class || (echo "Error: LWJGL 3.4 preedit callbacks missing from aggregate lwjgl.jar"; exit 1)
	@$(BOOTJDK)/jar -cf $@ -C $(OUTPUTDIR)/lwjgl_lib .
	@$(BOOTJDK)/jar -tf $@ | grep -q '^org/lwjgl/util/spvc/Spvc.class$$' || (echo "Error: generated lwjgl.jar does not contain Spvc.class"; exit 1)
	@$(BOOTJDK)/javap -classpath $@ org.lwjgl.system.Configuration | grep -q 'SPVC_LIBRARY_NAME' || (echo "Error: generated lwjgl.jar lacks Configuration.SPVC_LIBRARY_NAME"; exit 1)
	@$(BOOTJDK)/javap -classpath $@ org.lwjgl.glfw.GLFW | grep -q 'glfwPlatformSupported' || (echo "Error: generated lwjgl.jar lacks glfwPlatformSupported"; exit 1)
	@$(BOOTJDK)/javap -classpath $@ org.lwjgl.glfw.GLFW | grep -q 'glfwSetPreeditCallback' || (echo "Error: generated lwjgl.jar lacks GLFW 3.4 preedit callback ABI"; exit 1)

$(OUTPUTDIR)/patchjna_agent.jar: $(CLASSES)
	@set -e
	@mv $(basename $@)/com/sun/jna/Platform.class $(basename $@)/com/sun/jna/Platform.class.patch
	@echo "Creating $@"
	@$(BOOTJDK)/jar -cfm $@ $(SOURCEDIR)/patchjna_agent.txt -C $(basename $@) .

clean:
	rm -rf $(OUTPUTDIR)

.SUFFIXES: .java
.PHONY: all clean require_lwjgl_modules stage_lwjgl_natives stage_metallum_natives
