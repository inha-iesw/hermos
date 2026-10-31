#pragma once

namespace g3_fd_reply_failure
{
/** @brief Counts of the selected reply, accepted-FD or accept operation. */
struct Stats
{
  unsigned attempts;  ///< Selected calls, including later pass-through calls.
  unsigned injections;  ///< Calls changed by the selected failure mode.
  unsigned malformed_sent;  ///< Malformed sendmsg calls returning one byte; zero for other modes.
};

/**
 * @brief Observe the producer reply injection counters.
 *
 * G3_FD_REPLY_FAILURE selects two_fds, truncated, send_error, accept_eintr,
 * accept_error, accepted_getfd or accepted_setfd. Only the first three selected
 * operations fail; later operations pass through. Accept modes close a real
 * accepted connection before returning an injected errno, so startup is not
 * counted and no accepted FD escapes the wrapper.
 * @return A snapshot of the counters for the active selector; zero when disabled.
 */
Stats stats();
}  // namespace g3_fd_reply_failure
