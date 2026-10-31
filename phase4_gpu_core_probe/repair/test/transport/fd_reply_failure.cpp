#include "fd_reply_failure.hpp"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstddef>
#include <cstdarg>
#include <cstdlib>
#include <cstring>

namespace
{
enum class Failure
{
  None, TwoFds, Truncated, SendError, AcceptInterrupted, AcceptError,
  AcceptedGetFlags, AcceptedSetFlags
};

Failure selected_failure()
{
  static const Failure selected = []() {
      const char * value = std::getenv("G3_FD_REPLY_FAILURE");
      if (!value) {return Failure::None;}
      if (std::strcmp(value, "two_fds") == 0) {return Failure::TwoFds;}
      if (std::strcmp(value, "truncated") == 0) {return Failure::Truncated;}
      if (std::strcmp(value, "send_error") == 0) {return Failure::SendError;}
      if (std::strcmp(value, "accept_eintr") == 0) {return Failure::AcceptInterrupted;}
      if (std::strcmp(value, "accept_error") == 0) {return Failure::AcceptError;}
      if (std::strcmp(value, "accepted_getfd") == 0) {return Failure::AcceptedGetFlags;}
      if (std::strcmp(value, "accepted_setfd") == 0) {return Failure::AcceptedSetFlags;}
      return Failure::None;
    }();
  return selected;
}

std::atomic<unsigned> attempts{0};
std::atomic<unsigned> injections{0};
std::atomic<unsigned> malformed_sent{0};

bool core_socket(int socket)
{
  sockaddr_un address{};
  socklen_t length = sizeof(address);
  constexpr char prefix[] = "/tmp/cuda_memory_pool_";
  return getsockname(socket, reinterpret_cast<sockaddr *>(&address), &length) == 0 &&
         address.sun_family == AF_UNIX &&
         length >= offsetof(sockaddr_un, sun_path) + sizeof(prefix) - 1 &&
         std::strncmp(address.sun_path, prefix, sizeof(prefix) - 1) == 0;
}

bool core_reply(int socket, const msghdr * message)
{
  if (!message) {return false;}
  msghdr probe = *message;
  auto * header = CMSG_FIRSTHDR(&probe);
  if (!header || header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS ||
    header->cmsg_len != CMSG_LEN(sizeof(int)) || CMSG_NXTHDR(&probe, header))
  {
    return false;
  }
  return core_socket(socket);
}

bool inject_selected_call()
{
  if (attempts.fetch_add(1) >= 3) {return false;}
  injections.fetch_add(1);
  return true;
}
}  // namespace

namespace g3_fd_reply_failure
{
Stats stats()
{
  return {attempts.load(), injections.load(), malformed_sent.load()};
}
}  // namespace g3_fd_reply_failure

extern "C" ssize_t sendmsg(int socket, const msghdr * message, int flags)
{
  static const auto real = reinterpret_cast<decltype(&sendmsg)>(dlsym(RTLD_NEXT, "sendmsg"));
  if (!real) {errno = ENOSYS; return -1;}
  const auto selected = selected_failure();
  if ((selected != Failure::TwoFds && selected != Failure::Truncated &&
    selected != Failure::SendError) || !core_reply(socket, message))
  {
    return real(socket, message, flags);
  }
  if (!inject_selected_call()) {return real(socket, message, flags);}
  if (selected == Failure::SendError) {errno = EPIPE; return -1;}

  // Duplicate the sender-owned VMM FD in real SCM_RIGHTS data. The kernel
  // installs receiver-owned copies; neither the source FD nor message changes.
  int source = -1;
  std::memcpy(&source, CMSG_DATA(CMSG_FIRSTHDR(message)), sizeof(source));
  const unsigned count = selected == Failure::TwoFds ? 2 : 8;
  alignas(cmsghdr) char control[CMSG_SPACE(8 * sizeof(int))]{};
  msghdr reply = *message;
  reply.msg_control = control;
  reply.msg_controllen = CMSG_SPACE(count * sizeof(int));
  auto * header = CMSG_FIRSTHDR(&reply);
  header->cmsg_level = SOL_SOCKET;
  header->cmsg_type = SCM_RIGHTS;
  header->cmsg_len = CMSG_LEN(count * sizeof(int));
  for (unsigned index = 0; index < count; ++index) {
    std::memcpy(CMSG_DATA(header) + index * sizeof(int), &source, sizeof(source));
  }
  const auto result = real(socket, &reply, flags);
  if (result == 1) {malformed_sent.fetch_add(1);}
  return result;
}

// Select only completed accepts on a core listener, so startup and shutdown
// cannot consume the failure. Closing the real accepted FD before returning
// an injected errno models the server's error branch, not kernel FD exhaustion.
extern "C" int accept(int socket, sockaddr * address, socklen_t * length)
{
  static const auto real = reinterpret_cast<decltype(&accept)>(dlsym(RTLD_NEXT, "accept"));
  if (!real) {errno = ENOSYS; return -1;}
  const int client = real(socket, address, length);
  const auto selected = selected_failure();
  if (client >= 0 &&
    (selected == Failure::AcceptInterrupted || selected == Failure::AcceptError) &&
    core_socket(socket) && inject_selected_call())
  {
    close(client);
    errno = selected == Failure::AcceptInterrupted ? EINTR : EIO;
    return -1;
  }
  return client;
}

extern "C" int __real_fcntl(int descriptor, int command, ...);

// Link wrapping targets core/static-object calls; CUDA and other DSOs keep
// their original binding. The pinned core uses only these two fcntl commands.
extern "C" int __wrap_fcntl(int descriptor, int command, ...)
{
  const auto selected = selected_failure();
  const bool selected_command =
    (selected == Failure::AcceptedGetFlags && command == F_GETFD) ||
    (selected == Failure::AcceptedSetFlags && command == F_SETFD);
  if (selected_command && core_socket(descriptor)) {
    int listening = 0;
    socklen_t size = sizeof(listening);
    if (getsockopt(descriptor, SOL_SOCKET, SO_ACCEPTCONN, &listening, &size) == 0 &&
      listening == 0 && inject_selected_call())
    {
      errno = EIO;
      return -1;
    }
  }
  if (command == F_GETFD) {return __real_fcntl(descriptor, command);}
  if (command != F_SETFD) {std::abort();}
  va_list arguments;
  va_start(arguments, command);
  const int flags = va_arg(arguments, int);
  va_end(arguments);
  return __real_fcntl(descriptor, command, flags);
}
