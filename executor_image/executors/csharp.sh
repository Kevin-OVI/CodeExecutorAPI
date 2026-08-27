#!/bin/sh
set -euo pipefail

# Create a temporary project directory
mkdir -p /tmp/csharp_project
cd /tmp/csharp_project

# Create the Program.cs file with the user's code
printf '%s' "$1" > Program.cs

# Create a minimal project file if it doesn't exist
if [ ! -f "csharp_project.csproj" ]; then
    cat > csharp_project.csproj << 'EOF'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
EOF
fi

# Build and run
dotnet run --no-launch-profile
