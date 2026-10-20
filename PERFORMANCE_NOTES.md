# Performance Optimization Notes

## Recent Changes (2026-10-08)

### Network & Timeout Improvements
- **Increased all timeouts to 180 seconds (3 minutes)**
  - Read timeout: 60s → 180s
  - Write timeout: 60s → 180s
  - Pool timeout: 30s → 60s
- **Increased connection pool size**: 8 → 16 connections
- **Increased retry attempts**: 2 → 3 retries for file uploads
- **Better retry logic**: Now catches "Could not deliver" errors in addition to timeouts
- **Longer retry delays**: 2s → 3s between retry attempts

### Database Optimizations
- **Faster authorization checks**: Using `EXISTS()` instead of `SELECT 1`
- **Non-blocking last_seen updates**: Runs in background thread
- **Better error handling**: Database errors don't crash the bot
- **Connection retry logic**: 3 attempts with exponential backoff
- **Keepalive settings**: Prevents connection drops on idle

### User Experience
- **Progress indicators**: Shows "⬇️ Downloading" and "⬆️ Uploading" with attempt numbers
- **Better error messages**: Users know when system is retrying
- **Status updates**: Real-time feedback during long operations

## Typical Processing Times

### Small Files (<1MB):
- Download: 1-3 seconds
- Processing: 2-5 seconds
- Upload: 1-3 seconds
- **Total: ~5-10 seconds**

### Medium Files (1-10MB):
- Download: 3-10 seconds
- Processing: 10-30 seconds
- Upload: 5-15 seconds
- **Total: ~20-60 seconds**

### Large Files (10-50MB):
- Download: 10-30 seconds
- Processing: 30-120 seconds
- Upload: 15-60 seconds
- **Total: ~1-3 minutes**

## Common Issues & Solutions

### "Telegram could not deliver the file"
**Causes:**
- Network timeout between Railway and Telegram servers
- File too large for Telegram's network buffers
- Telegram server temporarily overloaded

**Solutions Applied:**
- ✅ Increased timeouts to 180s (3 minutes)
- ✅ Added 3 retry attempts with 3s delay
- ✅ Stream position reset between retries
- ✅ Progress indicators to keep connection alive

### Bot Slowness
**Causes:**
- Database connection overhead
- Synchronous database queries
- Too many concurrent operations

**Solutions Applied:**
- ✅ Non-blocking last_seen updates
- ✅ Optimized authorization checks with EXISTS()
- ✅ Connection pooling with 16 connections
- ✅ Background task execution for DB updates

### Multiple Bot Instances Conflict
**Causes:**
- Railway deploying multiple replicas
- Old deployments not stopped

**Solutions Applied:**
- ✅ Set `numReplicas: 1` in railway.json
- ✅ Proper deployment cleanup in Railway dashboard

## Railway Resource Allocation

Recommended settings:
- **Memory**: 512MB minimum (1GB recommended for large files)
- **CPU**: Shared OK (Dedicated better for high volume)
- **Region**: Choose closest to your users

## Monitoring

Watch these metrics:
- Response time per file size
- Retry rate
- Database connection pool usage
- Memory usage during file processing

## Future Improvements

Potential optimizations:
1. Implement file chunking for uploads >25MB
2. Add Redis caching for frequent authorization checks
3. Use webhook instead of polling for faster response
4. Compress files before upload if possible
5. Parallel processing for multiple layers
