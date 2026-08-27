# roots requested outputs for nix-eval-jobs
# quoted placeholders keep this template parseable before substitution
# generated literals contain only allowlisted or escaped values
flake:
let
  o = flake.outputs;
  systems = [ "@SYSTEMS@" ];
  perSystemSets = [ "@PER_SYSTEM@" ];
  configSets = [ "@CONFIG@" ];
  leafSets = [ "@LEAF@" ];
  # system entries contain concrete trees after "*" folding
  # configuration entries contain flat host trees
  excludes = "@EXCLUDES@";
  # recursion below the deepest include path cannot produce a selected job
  maxDepth = "@MAXDEPTH@";
  startDepth = if maxDepth > 3 then maxDepth - 3 else 0;
  # attrset scope plumbing that cannot contain buildable leaves
  denylist = [ "@DENYLIST@" ];
  dropped = excl: builtins.filter (n: excl.${n} == true) (builtins.attrNames excl);
  # remove excludes before probing values so excluded thunks are never forced
  # failed probes stay as leaves for nix-eval-jobs to report
  # attrsets recurse only within the include depth budget
  sanitize = remaining: excl: set:
    let
      cleaned = builtins.removeAttrs set (denylist ++ dropped excl);
    in
    builtins.listToAttrs (builtins.concatMap
      (name:
        let
          raw = cleaned.${name};
          probe = builtins.tryEval (
            if builtins.isFunction raw then "fn"
            else if (raw.type or null) == "derivation" then "drv"
            else if builtins.isAttrs raw then
              (if (raw.recurseForDerivations or true) == false then "norec" else "attrs")
            else "other"
          );
          kind = if probe.success then probe.value else "throws";
        in
        if kind == "drv" || kind == "throws"
        then [{ inherit name; value = raw; }]
        else if kind == "attrs" && remaining > 0
        then [{ inherit name; value = sanitize (remaining - 1) (excl.${name} or { }) raw; }]
        else [ ]
      )
      (builtins.attrNames cleaned));
  ps = builtins.foldl'
    (acc: set:
      builtins.foldl'
        (a: sys:
          if (o ? ${set}) && (o.${set} ? ${sys})
          then a // { "${set}.${sys}" = sanitize startDepth (excludes.${set}.${sys} or { }) o.${set}.${sys}; }
          else a
        )
        acc
        systems
    )
    { }
    perSystemSets;
  # remove excluded hosts before mapAttrs can force their configurations
  cs = builtins.foldl'
    (acc: set:
      if o ? ${set}
      then acc // {
        "${set}" = builtins.mapAttrs (_: c: c.config.system.build.toplevel)
          (builtins.removeAttrs o.${set} (dropped (excludes.${set} or { })));
      }
      else acc
    )
    { }
    configSets;
  # leaf set values must be derivations and cannot be recursively sanitized
  ls = builtins.foldl'
    (acc: set:
      builtins.foldl'
        (a: sys:
          if (o ? ${set}) && (o.${set} ? ${sys})
          then a // {
            "${set}.${sys}" =
              let v = o.${set}.${sys}; in
              if (v.type or null) == "derivation" then v
              else throw "flake output ${set}.${sys} is not a derivation";
          }
          else a
        )
        acc
        systems
    )
    { }
    leafSets;
in
ps // cs // ls
