from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Enum
from sqlalchemy.sql import func
from sqlalchemy.future import select
from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Optional
import enum
import os
from dotenv import load_dotenv

load_dotenv()

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./inventory.db")

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False  # Changed to False for production safety
)

AsyncSessionLocal = async_sessionmaker(
    autocommit=False,
    autoflush=False, 
    bind=engine,
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

# Models
class TransactionType(enum.Enum):
    IN = "IN"
    OUT = "OUT"

class Product(Base):
    __tablename__ = "products"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String)
    price = Column(Float, nullable=False)
    available_quantity = Column(Integer, nullable=False, default=0)
    
    stock_transactions = relationship("StockTransaction", back_populates="product")

class StockTransaction(Base):
    __tablename__ = "stock_transactions"
    
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    transaction_type = Column(Enum(TransactionType), nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    
    product = relationship("Product", back_populates="stock_transactions")

# Pydantic schemas
class TransactionTypeEnum(str, enum.Enum):
    IN = "IN"
    OUT = "OUT"

class ProductBase(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    price: float = Field(..., gt=0)

class ProductCreate(ProductBase):
    available_quantity: int = Field(default=0, ge=0)

class ProductUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None
    price: Optional[float] = Field(None, gt=0)
    available_quantity: Optional[int] = Field(None, ge=0)

class ProductResponse(ProductBase):
    id: int
    available_quantity: int
    
    class Config:
        from_attributes = True

class StockTransactionCreate(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0)
    transaction_type: TransactionTypeEnum

class StockTransactionResponse(BaseModel):
    id: int
    product_id: int
    quantity: int
    transaction_type: TransactionTypeEnum
    timestamp: datetime
    
    class Config:
        from_attributes = True

# Database dependency
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# FastAPI app
app = FastAPI(title="Inventory Management API", version="1.0.0")

# Product endpoints
@app.post("/products/", response_model=ProductResponse, status_code=201)
async def create_product(product: ProductCreate, db: AsyncSession = Depends(get_db)):
    db_product = Product(**product.model_dump())
    db.add(db_product)
    await db.commit()
    await db.refresh(db_product)
    return db_product

@app.get("/products/", response_model=List[ProductResponse])
async def list_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Product).offset(skip).limit(limit))
    return result.scalars().all()

@app.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(product_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product

@app.put("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: int, 
    product_update: ProductUpdate, 
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    update_data = product_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(product, key, value)
    
    await db.commit()
    await db.refresh(product)
    return product

@app.delete("/products/{product_id}")
async def delete_product(product_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    await db.delete(product)
    await db.commit()
    return {"message": "Product deleted successfully"}

# Stock transaction endpoints
@app.post("/stock/", response_model=StockTransactionResponse, status_code=201)
async def create_stock_transaction(
    transaction: StockTransactionCreate,
    db: AsyncSession = Depends(get_db)
):
    try:
        # Start a transaction and lock the product row to prevent race conditions
        async with db.begin():
            # Get product with row-level lock (FOR UPDATE)
            result = await db.execute(
                select(Product)
                .where(Product.id == transaction.product_id)
                .with_for_update()
            )
            product = result.scalar_one_or_none()
            if not product:
                raise HTTPException(status_code=404, detail="Product not found")
            
            # Update product quantity
            if transaction.transaction_type == TransactionTypeEnum.IN:
                product.available_quantity += transaction.quantity
            else:  # OUT
                if product.available_quantity < transaction.quantity:
                    raise HTTPException(status_code=400, detail="Insufficient stock")
                product.available_quantity -= transaction.quantity
            
            # Create transaction
            db_transaction = StockTransaction(
                product_id=transaction.product_id,
                quantity=transaction.quantity,
                transaction_type=TransactionType(transaction.transaction_type.value)
            )
            
            db.add(db_transaction)
            await db.flush()  # Flush to get the ID
            await db.refresh(db_transaction)
            return db_transaction
            
    except Exception as e:
        await db.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail="Internal server error during stock transaction")

@app.get("/stock/", response_model=List[StockTransactionResponse])
async def list_stock_transactions(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(StockTransaction)
        .order_by(StockTransaction.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()

@app.get("/stock/product/{product_id}", response_model=List[StockTransactionResponse])
async def get_product_transactions(
    product_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db)
):
    # Check if product exists
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    # Get transactions
    result = await db.execute(
        select(StockTransaction)
        .where(StockTransaction.product_id == product_id)
        .order_by(StockTransaction.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()

@app.get("/")
async def root():
    return {"message": "Inventory Management API - CoreZen Solutions"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)