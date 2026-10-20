/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    const defaultBackend = process.env.NODE_ENV === "production" ? "http://api:8000" : "http://localhost:8000";
    const backendUrl = process.env.INTERNAL_API_URL || defaultBackend;
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;

