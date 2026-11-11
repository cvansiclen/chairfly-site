// Runs automatically every time a Netlify form on chairflytrainer.com gets a
// (spam-checked) submission. For the "ipad-link" form, it emails the visitor
// the ChairFly App Store link so they can open it on their iPad.
//
// Needs two environment variables in Netlify (Site configuration > Environment variables):
//   SMTP_USER  = info@chairflytrainer.com
//   SMTP_PASS  = a Google app password for that account (not the normal password)

const nodemailer = require("nodemailer");

const APP_STORE_URL = "https://apps.apple.com/app/chairfly/id6805243067";

exports.handler = async (event) => {
  let payload;
  try {
    payload = JSON.parse(event.body).payload;
  } catch (e) {
    return { statusCode: 400, body: "Bad payload" };
  }

  if (!payload || payload.form_name !== "ipad-link") {
    return { statusCode: 200, body: "Not the ipad-link form; nothing to do." };
  }

  const to = (payload.data && payload.data.email) || payload.email;
  if (!to) return { statusCode: 200, body: "No email address." };

  if (!process.env.SMTP_USER || !process.env.SMTP_PASS) {
    console.log("SMTP_USER / SMTP_PASS not set; submission saved but no email sent.");
    return { statusCode: 200, body: "Email not configured." };
  }

  const transporter = nodemailer.createTransport({
    service: "gmail",
    auth: { user: process.env.SMTP_USER, pass: process.env.SMTP_PASS },
  });

  const text = [
    "Here's your link to ChairFly. Open this email on your iPad and tap the link:",
    "",
    APP_STORE_URL,
    "",
    "ChairFly is free to download. Tilt your iPad to fly, run the scan, and get your reps on the ground.",
    "",
    "Cheers,",
    "ChairFly",
    "chairflytrainer.com",
  ].join("\n");

  const html = `
  <div style="background:#f6f4ef;padding:32px 16px;font-family:Helvetica,Arial,sans-serif;">
    <div style="max-width:480px;margin:0 auto;background:#0e1951;border-radius:14px;padding:32px 28px;color:#f6f4ef;">
      <img src="https://chairflytrainer.com/email-logo.png" alt="ChairFly" width="64" style="display:block;border-radius:12px;margin-bottom:20px;">
      <p style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#e8a33d;margin:0 0 8px;">iPad Exclusive</p>
      <h1 style="font-size:26px;line-height:1.15;margin:0 0 14px;">Here's ChairFly for your iPad.</h1>
      <p style="font-size:15px;line-height:1.5;color:rgba(246,244,239,0.85);margin:0 0 24px;">Open this email on your iPad and tap the button below. ChairFly is free to download. Tilt your iPad to fly, run the scan, and get your reps on the ground.</p>
      <a href="${APP_STORE_URL}" style="display:inline-block;background:#e8a33d;color:#070c28;font-weight:bold;text-decoration:none;padding:14px 24px;border-radius:8px;font-size:15px;">Open ChairFly on the App Store</a>
      <p style="font-size:15px;line-height:1.5;color:rgba(246,244,239,0.85);margin:28px 0 0;">Cheers,<br>ChairFly</p>
    </div>
    <p style="max-width:480px;margin:14px auto 0;text-align:center;font-size:11px;color:#5b6584;">You're getting this because you asked for the ChairFly link at chairflytrainer.com.</p>
  </div>`;

  try {
    await transporter.sendMail({
      from: `"ChairFly" <${process.env.SMTP_USER}>`,
      to,
      subject: "Your ChairFly link for iPad",
      text,
      html,
    });
    return { statusCode: 200, body: "Sent" };
  } catch (err) {
    console.error("Email send failed:", err && err.message);
    return { statusCode: 200, body: "Send failed" };
  }
};
