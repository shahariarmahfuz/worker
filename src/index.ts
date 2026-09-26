export default {
  async email(message, env, ctx) {
    const backendUrl =
      "https://next-net-rack-arabic.trycloudflare.com/api/internal/email/incoming";

    try {
      const response = await fetch(backendUrl, {
        method: "POST",
        headers: {
          "Content-Type": "message/rfc822",
          "X-Email-From": message.from,
          "X-Email-To": message.to,
          "X-Email-Source": "cloudflare",
          "X-Request-Id": crypto.randomUUID(),
        },
        body: message.raw,
      });

      if (!response.ok) {
        console.error(
          `Backend error: ${response.status}`
        );

        message.setReject("Email delivery failed");
        return;
      }

      console.log(
        `Email forwarded: ${message.from} -> ${message.to}`
      );
    } catch (error) {
      console.error("Forwarding failed:", error);

      message.setReject("Temporary delivery failure");
    }
  },
};
