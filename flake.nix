{
  description = "Python dev shell (uv-managed)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            ruff
          ];

          env = {
            # Prevent uv from managing Python itself; let it use whatever's on PATH
            # UV_PYTHON_PREFERENCE = "only-system";
          };

          shellHook = ''
            # Sync deps and activate the venv uv manages
            uv sync --quiet
            source .venv/bin/activate
          '';
        };
      });
}
