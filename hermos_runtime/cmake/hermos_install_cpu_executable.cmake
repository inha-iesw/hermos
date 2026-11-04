# Install an explicitly opted-in Python entry or native target behind the CPU bootstrap.
function(hermos_install_cpu_executable name)
  cmake_parse_arguments(ARG "" "PYTHON_ENTRY;TARGET" "PROVIDERS" ${ARGN})
  if(ARG_UNPARSED_ARGUMENTS OR NOT name MATCHES "^[a-zA-Z0-9_]+$")
    message(FATAL_ERROR "hermos_install_cpu_executable requires a simple executable name")
  endif()
  if((ARG_PYTHON_ENTRY AND ARG_TARGET) OR (NOT ARG_PYTHON_ENTRY AND NOT ARG_TARGET))
    message(FATAL_ERROR "Select exactly one of PYTHON_ENTRY or TARGET")
  endif()
  set(providers "[")
  foreach(provider IN LISTS ARG_PROVIDERS)
    if(NOT provider MATCHES "^[a-z][a-z0-9_]*$")
      message(FATAL_ERROR "Invalid adapter provider: ${provider}")
    endif()
    string(APPEND providers "'${provider}',")
  endforeach()
  string(APPEND providers "]")
  if(ARG_PYTHON_ENTRY)
    if(NOT ARG_PYTHON_ENTRY MATCHES "^[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*$")
      message(FATAL_ERROR "PYTHON_ENTRY must be module:function")
    endif()
    set(body "from hermos_runtime.cpu import run\nraise SystemExit(run('${ARG_PYTHON_ENTRY}', providers=${providers}))\n")
  else()
    if(NOT TARGET "${ARG_TARGET}")
      message(FATAL_ERROR "Unknown CPU executable target: ${ARG_TARGET}")
    endif()
    # ros2 run searches lib/<package> recursively; keep the unwrapped binary
    # outside that tree so executable discovery exposes only the bootstrap.
    install(TARGETS ${ARG_TARGET} RUNTIME DESTINATION "libexec/${PROJECT_NAME}")
    set(body "from pathlib import Path\nfrom hermos_runtime.cpu import run_executable\nraise SystemExit(run_executable(Path(__file__).parents[2] / 'libexec' / '${PROJECT_NAME}' / '$<TARGET_FILE_NAME:${ARG_TARGET}>', providers=${providers}))\n")
  endif()
  file(GENERATE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/cpu-wrappers/${name}"
    CONTENT "#!/usr/bin/python3\n${body}"
    FILE_PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE)
  install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/cpu-wrappers/${name}"
    DESTINATION "lib/${PROJECT_NAME}"
    PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE)
endfunction()
