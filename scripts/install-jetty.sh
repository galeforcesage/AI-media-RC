#!/bin/bash
# Install SageTV Jetty Webserver V3 plugin and dependencies
# Run inside the SageTV Docker container
set -e

SAGETV_DIR="/opt/sagetv/server"
JARS_DIR="$SAGETV_DIR/JARs"
PLUGIN_XML="$SAGETV_DIR/SageTVPluginsV9.xml"
TMP_DIR="/tmp/jetty-install"

mkdir -p "$TMP_DIR"
cd "$TMP_DIR"

# Install wget and unzip if missing
apt-get update -qq
apt-get install -y -qq wget unzip > /dev/null 2>&1

echo "=== Extracting dependency plugin URLs from SageTVPluginsV9.xml ==="

# Function to extract all package URLs for a given plugin Identifier
get_plugin_packages() {
    local plugin_id="$1"
    python3 -c "
import xml.etree.ElementTree as ET
import sys

tree = ET.parse('$PLUGIN_XML')
root = tree.getroot()

for plugin in root.findall('.//SageTVPlugin'):
    ident = plugin.find('Identifier')
    if ident is not None and ident.text == '$plugin_id':
        plugin_type = plugin.find('PluginType')
        # Skip if PluginType is not Standard or Library
        if plugin_type is not None and plugin_type.text not in ('Standard', 'Library'):
            continue
        for pkg in plugin.findall('Package'):
            pkg_type = pkg.find('PackageType')
            location = pkg.find('Location')
            overwrite = pkg.find('Overwrite')
            ow = overwrite.text if overwrite is not None else 'true'
            if location is not None:
                print(f'{pkg_type.text}|{location.text}|{ow}')
        break
" 2>/dev/null || echo "PARSE_ERROR"
}

# Check if python3 is available
if ! command -v python3 &> /dev/null; then
    echo "python3 not found, installing..."
    apt-get install -y -qq python3 > /dev/null 2>&1
fi

echo ""
echo "=== Installing dependency plugins ==="

# Dependencies: sagex-api, ant, slf4j-api, slf4j-log4j12, log4j
DEPS="sagex-api ant slf4j-api slf4j-log4j12 log4j"

for dep in $DEPS; do
    echo "--- Checking dependency: $dep ---"
    packages=$(get_plugin_packages "$dep")
    if [ -z "$packages" ] || [ "$packages" = "PARSE_ERROR" ]; then
        echo "WARNING: Could not find plugin '$dep' in repository XML"
        continue
    fi
    
    while IFS='|' read -r pkg_type url overwrite; do
        echo "  Downloading: $url"
        filename=$(basename "$url")
        wget -q "$url" -O "$TMP_DIR/$filename"
        
        case "$pkg_type" in
            JAR)
                echo "  Extracting JARs to $JARS_DIR"
                unzip -o -q "$TMP_DIR/$filename" -d "$JARS_DIR"
                ;;
            System)
                echo "  Extracting System files to $SAGETV_DIR"
                if [ "$overwrite" = "false" ]; then
                    # Don't overwrite existing files
                    unzip -n -q "$TMP_DIR/$filename" -d "$SAGETV_DIR"
                else
                    unzip -o -q "$TMP_DIR/$filename" -d "$SAGETV_DIR"
                fi
                ;;
        esac
        rm -f "$TMP_DIR/$filename"
    done <<< "$packages"
done

echo ""
echo "=== Installing Jetty Web Server V3 plugin ==="

packages=$(get_plugin_packages "Jetty")
if [ -z "$packages" ] || [ "$packages" = "PARSE_ERROR" ]; then
    echo "ERROR: Could not find Jetty plugin in repository XML"
    exit 1
fi

while IFS='|' read -r pkg_type url overwrite; do
    echo "  Downloading: $url"
    filename=$(basename "$url")
    wget -q "$url" -O "$TMP_DIR/$filename"
    
    case "$pkg_type" in
        JAR)
            echo "  Extracting JARs to $JARS_DIR"
            unzip -o -q "$TMP_DIR/$filename" -d "$JARS_DIR"
            ;;
        System)
            echo "  Extracting System files to $SAGETV_DIR"
            if [ "$overwrite" = "false" ]; then
                unzip -n -q "$TMP_DIR/$filename" -d "$SAGETV_DIR"
            else
                unzip -o -q "$TMP_DIR/$filename" -d "$SAGETV_DIR"
            fi
            ;;
    esac
    rm -f "$TMP_DIR/$filename"
done <<< "$packages"

echo ""
echo "=== Updating Sage.properties ==="

# Add Jetty plugin to Sage.properties if not already there
SAGE_PROPS="$SAGETV_DIR/Sage.properties"

if ! grep -q "load_at_startup_runnable_classes" "$SAGE_PROPS" 2>/dev/null; then
    echo "load_at_startup_runnable_classes=sagex.jetty.starter.JettyPlugin" >> "$SAGE_PROPS"
    echo "  Added JettyPlugin to startup classes"
elif ! grep -q "sagex.jetty.starter.JettyPlugin" "$SAGE_PROPS" 2>/dev/null; then
    # Append to existing startup classes
    current=$(grep "load_at_startup_runnable_classes=" "$SAGE_PROPS" | head -1 | cut -d'=' -f2)
    if [ -z "$current" ]; then
        sed -i "s|load_at_startup_runnable_classes=|load_at_startup_runnable_classes=sagex.jetty.starter.JettyPlugin|" "$SAGE_PROPS"
    else
        sed -i "s|load_at_startup_runnable_classes=$current|load_at_startup_runnable_classes=$current;sagex.jetty.starter.JettyPlugin|" "$SAGE_PROPS"
    fi
    echo "  Appended JettyPlugin to startup classes"
else
    echo "  JettyPlugin already in startup classes"
fi

# Set default Jetty port if not configured
if ! grep -q "jetty/port" "$SAGE_PROPS" 2>/dev/null; then
    echo "jetty/port=8080" >> "$SAGE_PROPS"
    echo "  Set Jetty port to 8080"
fi

echo ""
echo "=== Verifying installation ==="
echo "JARs directory:"
ls -la "$JARS_DIR/" | grep -i -E "jetty|sagex|ant|slf4j|log4j"
echo ""
echo "Jetty config directory:"
ls -la "$SAGETV_DIR/jetty/" 2>/dev/null || echo "  (jetty config dir not found)"
echo ""
echo "Sage.properties entries:"
grep -i -E "jetty|load_at_startup" "$SAGE_PROPS" | head -10

# Cleanup
rm -rf "$TMP_DIR"

echo ""
echo "=== Installation complete ==="
echo "SageTV needs to be restarted for Jetty to start."
echo "After restart, Jetty should be available at http://localhost:8080/"
